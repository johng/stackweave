"""Dedicated offload hubs (mn_init(offload_hubs=K) / STACKWEAVE_OFFLOAD_HUBS).

The mechanism: reserve K hubs at the tail of runloom_hubs[], exclude them from
general placement / stealing / sysmon preemption / the monopoly-yield scan, and
run blocking calls there as ordinary fibers.  That lets `monkey.offload` reuse
the scheduler (spawn, submit, channel, wake_g) instead of the bespoke thread
pool + self-pipe + result-box protocol, and -- because nothing ever migrates
between hubs -- it needs no CPython tstate patches.  See the STACKWEAVE_OFFLOAD_HUBS
block in src/runloom_c/mn_sched.c.

These run IN-PROCESS: `offload_hubs` is an mn_init argument, so K can vary per
run().  (The STACKWEAVE_OFFLOAD_HUBS env fallback is resolved once per process and
could not be varied without re-execing a subprocess per case.)

`time.sleep` is the stand-in for a blocking call: it releases the tstate, so a
hub sitting in it is DETACHED-with-work, exactly like a blocking syscall.
"""
import time

import pytest

import stackweave
import stackweave_c as rc
from stackweave.monkey import offload


# --------------------------------------------------------------------------
# Reservation and accounting.

def test_default_is_off():
    """No argument, no env -> no offload hubs, and offload_fiber REFUSES rather
    than silently falling back to a general hub (a blocking call there would
    strand every fiber woken on it)."""
    seen = {}

    def body():
        seen["hubs"] = rc.mn_hub_count()
        seen["offload"] = rc.offload_hub_count()
        with pytest.raises(RuntimeError):
            rc.offload_fiber(lambda: None)

    stackweave.run(4, body, offload_hubs=0)
    assert seen == {"hubs": 4, "offload": 0}


def test_hubs_are_added_not_carved_out():
    """K is ADDED to n: reserving offload capacity must never silently reduce
    general parallelism."""
    seen = {}

    def body():
        seen["hubs"] = rc.mn_hub_count()
        seen["offload"] = rc.offload_hub_count()

    stackweave.run(4, body, offload_hubs=3)
    assert seen == {"hubs": 7, "offload": 3}


def test_k_varies_per_run_in_process():
    """The whole reason offload_hubs is an argument and not only an env var:
    the env read is resolved once per process, so it cannot express this."""
    got = []

    def make(k):
        def body():
            got.append((k, rc.offload_hub_count()))
        return body

    for k in (0, 2, 1, 4):
        stackweave.run(2, make(k), offload_hubs=k)
    assert got == [(0, 0), (2, 2), (1, 1), (4, 4)]


# --------------------------------------------------------------------------
# The isolation invariant: no general work on an offload hub.

def test_general_fibers_never_land_on_a_blocked_offload_hub():
    """Oracle without a current-hub API: saturate every offload hub with a long
    blocking call, then spawn a batch of ordinary fibers that only set a flag.
    A general fiber placed on (or stolen onto) an offload hub could not run
    until the block cleared -- so "all finished well before it cleared" is the
    exclusion holding."""
    K, N, BLOCK = 2, 400, 3.0
    done = bytearray(N)          # one slot each: a shared += loses updates GIL-off
    started = time.monotonic()

    def body():
        for _ in range(K):
            rc.offload_fiber(lambda: time.sleep(BLOCK))
        stackweave.sleep(0.25)      # let every offload hub reach its sleep

        def worker(i):
            def f():
                done[i] = 1
            return f
        for i in range(N):
            stackweave.fiber(worker(i))

        deadline = started + BLOCK - 0.5
        while time.monotonic() < deadline and sum(done) < N:
            stackweave.sleep(0.02)

    stackweave.run(4, body, offload_hubs=K)
    elapsed = time.monotonic() - started
    assert sum(done) == N, "%d/%d general fibers stalled behind a blocked offload hub" % (sum(done), N)
    assert elapsed < BLOCK + 1.0


def test_general_hubs_progress_while_every_offload_hub_blocks():
    """What the thread pool used to buy, now bought with the scheduler."""
    K = 2
    ticks = bytearray(4)

    def body():
        for _ in range(K):
            rc.offload_fiber(lambda: time.sleep(2.0))
        stackweave.sleep(0.25)

        stop = [False]

        def ticker(i):
            def f():
                while not stop[0]:
                    ticks[i] = 1
                    stackweave.sleep(0.01)
            return f
        for i in range(4):
            stackweave.fiber(ticker(i))

        stackweave.sleep(1.0)       # a full second WHILE the offload hubs block
        stop[0] = True
        stackweave.sleep(0.1)

    stackweave.run(4, body, offload_hubs=K)
    assert sum(ticks) == 4, "only %d/4 general tickers ran while offload hubs blocked" % sum(ticks)


# --------------------------------------------------------------------------
# monkey.offload routing.  The differential test below is the important one:
# it caught the hub path unpacking Chan.recv() backwards -- recv() is
# (value, ok), not (ok, value) -- which made every offload return True and
# swallowed every exception, while the pool path stayed correct.

def _probe():
    """Exercise return value, exception propagation and real blocking."""
    out = {}
    out["value"] = offload(lambda: sum(range(1000)))
    try:
        offload(lambda: (_ for _ in ()).throw(ValueError("boom")))
    except ValueError as exc:
        out["exc"] = str(exc)
    t0 = time.monotonic()
    out["slept"] = offload(lambda: (time.sleep(0.2), "done")[1])
    out["blocked_for_real"] = (time.monotonic() - t0) >= 0.15
    return out


@pytest.mark.parametrize("k,route", [(2, "hub"), (0, "pool")])
def test_offload_routes_and_behaves(k, route):
    seen = {}

    def body():
        seen["route"] = "hub" if rc.offload_hub_count() > 0 else "pool"
        seen.update(_probe())

    stackweave.run(3, body, offload_hubs=k)
    assert seen["route"] == route
    assert seen["value"] == 499500
    assert seen["exc"] == "boom"
    assert seen["slept"] == "done"
    assert seen["blocked_for_real"]


def test_both_backends_are_observationally_identical():
    """Swapping backends must not change what a caller sees -- same value, same
    exception, same blocking.  Guards the result/exception marshalling."""
    results = {}

    def run_with(k, key):
        def body():
            results[key] = _probe()
        stackweave.run(3, body, offload_hubs=k)

    run_with(2, "hub")
    run_with(0, "pool")
    assert results["hub"] == results["pool"], results


def test_offload_outside_a_fiber_runs_inline():
    """Foreign OS threads and plain non-fiber callers must never park a
    non-existent g -- checked before any scheduler state is touched."""
    assert offload(lambda: 7) == 7


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
