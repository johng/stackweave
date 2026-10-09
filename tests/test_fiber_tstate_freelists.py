"""Tearing down a finished fiber's per-g PyThreadState must stay cheap and
leak nothing, however many fibers the process has already run.

Two separate costs, both in ``runloom_iframe_clear_fiber_tstate``:

The leak.  Free-threaded CPython caches freed ints, floats, tuples, lists,
dicts, ... on per-thread-state freelists, and ``PyThreadState_Clear`` drains
the CURRENT thread state's freelists, not those of the state it is clearing
(CPython's own threads always clear themselves).  Every M:N fiber has its own
thread state, cleared by whichever thread drops the last reference to the
fiber -- so whatever a fiber had cached leaked with it.  The leaked blocks
stay "used" in the hub heap they were allocated from (alloc-home); when that
hub's thread state goes away at ``mn_fini`` its segments are abandoned to the
interpreter, and a segment pinned by leaked blocks can never be reclaimed, so
the abandoned pool only grew.  The grow-down sampler made it easy to hit:
every sampled spawn (and a fresh closure or lambda is sampled on every spawn)
computes a few non-small ints on the way out, about seven leaked blocks per
fiber.

The sweep.  Each ``PyThreadState_Clear`` abandons the state's four mimalloc
heaps, and each abandon also sweeps the interpreter's abandoned-segment pool
(up to 1024 segment walks per heap whenever one segment there still holds a
live block).  CPython pays that once per thread exit; a fiber paid it once
per fiber.  With the leak growing the pool, one teardown reached ~0.7 ms after
~75k ``stackweave.fiber`` children, so a hub fed one fiber per 300 us fell
behind for good: spawn->first-run latency onto an idle hub went from ~250 us
to tens of milliseconds, for the rest of the process.  Even without the leak,
whatever a fiber legitimately leaves alive keeps the pool non-empty after an
``mn_fini``, and a quiet hub then spent ~80 us per trivial fiber instead of ~5.

Each scenario runs in a fresh subprocess: the damage is process-wide and
permanent, so it must not leak into (or out of) the rest of the suite.
"""
import sys

import pytest

from adv_util import run_python

SCENARIO = r'''
import gc, sys, time
import stackweave, stackweave_c

HUBS = 4


def remote_spawn_p50_us(n=200, gap=0.0003):
    """p50 of spawn->first-run for mn_fiber(hub=1) from a fiber on hub 0,
    hub 1 otherwise idle (bench.mnsched 'spawn->run remote idle hub')."""
    lat = []
    stackweave_c.mn_init(HUBS)
    try:
        def spawner():
            for _ in range(n):
                t0 = time.perf_counter_ns()

                def f(t0=t0):
                    lat.append(time.perf_counter_ns() - t0)
                stackweave_c.mn_fiber(f, hub=1)
                stackweave_c.sched_sleep(gap)
        stackweave_c.mn_fiber(spawner, hub=0)
        stackweave_c.mn_run()
    finally:
        stackweave_c.mn_fini()
    assert len(lat) == n, len(lat)
    lat.sort()
    return lat[n // 2] / 1e3


def remote_drain_us_per_fiber(k=500):
    """Spawn k trivial fibers onto idle hub 1 in one burst from hub 0; the
    time hub 1 takes per fiber (run + teardown), best of three bursts."""
    best = None
    for _ in range(3):
        t = []
        stackweave_c.mn_init(HUBS)
        try:
            def spawner():
                t.append(time.perf_counter_ns())
                for _ in range(k):
                    stackweave_c.mn_fiber(lambda: t.append(time.perf_counter_ns()), hub=1)
            stackweave_c.mn_fiber(spawner, hub=0)
            stackweave_c.mn_run()
        finally:
            stackweave_c.mn_fini()
        assert len(t) == k + 1, len(t)
        per = (max(t) - t[0]) / k / 1e3
        best = per if best is None else min(best, per)
    return best


def forkjoin(rounds=50, width=100):
    """One hub pool: a root fiber runs `rounds` WaitGroup fork-joins of `width`
    stackweave.fiber children.  Each child is a fresh lambda, so grow-down
    samples (wraps and measures) every one of them."""
    stackweave_c.mn_init(HUBS)
    try:
        def root():
            for _ in range(rounds):
                wg = stackweave.WaitGroup()
                wg.add(width)
                for _ in range(width):
                    stackweave.fiber(lambda wg=wg: wg.done())
                wg.wait()
        stackweave_c.mn_fiber(root)
        stackweave_c.mn_run()
    finally:
        stackweave_c.mn_fini()
    return rounds * width


def allocated_blocks():
    gc.collect()
    return sys.getallocatedblocks()   # includes the abandoned-segment pool


assert stackweave.grow_down_enabled()
before = remote_spawn_p50_us()
drain_before = remote_drain_us_per_fiber()
forkjoin()                            # warm: imports, TLBC copies, caches
b0 = allocated_blocks()
fibers = 0
for _ in range(15):
    fibers += forkjoin()
b1 = allocated_blocks()
after = remote_spawn_p50_us()
drain_after = remote_drain_us_per_fiber()
print("RESULT before_us=%.1f after_us=%.1f drain_before_us=%.2f "
      "drain_after_us=%.2f leaked_per_fiber=%.3f fibers=%d"
      % (before, after, drain_before, drain_after, (b1 - b0) / fibers, fibers),
      flush=True)
'''


@pytest.fixture(scope="module")
def result():
    p = run_python(SCENARIO, timeout=120)
    assert p.returncode == 0, (p.returncode, p.stdout[-2000:], p.stderr[-4000:])
    line = [ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")]
    assert line, p.stdout[-2000:]
    return {k: float(v) for k, v in (kv.split("=") for kv in line[-1].split()[1:])}


def test_finished_fibers_leak_no_allocator_blocks(result):
    # ~7 blocks per fiber before the fix, ~0 after.  Whatever the fibers
    # created is gone by now, so anything left is per-fiber leakage.
    assert result["leaked_per_fiber"] < 1.0, (
        "finished fibers leaked %.2f allocator blocks each (%r): a per-g "
        "tstate's freelists were not freed with it"
        % (result["leaked_per_fiber"], result))


def test_remote_spawn_to_run_stays_fast_after_a_fiber_heavy_load(result):
    # Before the fix: ~250 us before the load, 11-18 ms after it.  Generous on
    # purpose -- the bug is a 40x+ regression that grows with the load.
    assert result["after_us"] <= max(10 * result["before_us"], 3000.0), (
        "remote spawn->first-run went from %.0f us to %.0f us after %d "
        "stackweave.fiber children (%r)"
        % (result["before_us"], result["after_us"], result["fibers"], result))


def test_fiber_teardown_does_not_slow_down_after_a_fiber_heavy_load(result):
    # An idle hub runs + tears down a trivial fiber in ~5 us in a fresh
    # process.  Sweeping the abandoned-segment pool on every teardown made it
    # ~80 us after the load even with the leak fixed (and ~0.7 ms with it).
    assert result["drain_after_us"] <= max(5 * result["drain_before_us"], 40.0), (
        "per-fiber run+teardown on an idle hub went from %.1f us to %.1f us "
        "after %d stackweave.fiber children (%r)"
        % (result["drain_before_us"], result["drain_after_us"],
           result["fibers"], result))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
