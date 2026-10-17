"""Bulk-spawn batch lifecycle at scale (docs/dev/spawn_experiments.md, Exp B).

Exercises the warm-stack arena + bulk + FRESH fast path: fiber_n(fn, N) builds
one big g/coro/stack batch, mn_run() drains all N, and the batch teardown returns
the slots.  Repeated to stress batch reset/reuse.  The gate runs this under
ASan/TSan, so a lifecycle UAF or a lost fiber surfaces here.

Under migration every M:N fiber runs on its own PyThreadState and is linked
in the fiber registry.  The bulk builder fills the g arena from threads that
hold no thread state, so runloom_mn_fiber_n_bulk (mn_sched_init_fini.c.inc)
makes each g's tstate and links the whole arena on the calling thread, before
it publishes the batch; mn_fini unlinks the arena before freeing it.  The bulk
path is observable only one way: STACKWEAVE_GON_TIMING=1 makes the builder
print one "[GON_TIMING]" line per batch it built, and every test here that
needs the bulk path counts those lines.

The stack arena the bulk path carves from (STACKWEAVE_STACK_ARENA) does work
on its own as well; test_stack_arena_fiber_n_lifecycle covers it through
fiber_n's per-fiber loop.

Every case runs in its own subprocess: the gates are read once per process, and
the low-level mn_init/mn_fini here must never share runtime state with the
high-level stackweave.run tests.
"""
import sys
import textwrap

import pytest

from adv_util import run_python


# The warm-stack arena + bulk + FRESH config the fast path was validated with.
BULK_ENV = {
    "STACKWEAVE_STACK_ARENA": "1",
    "STACKWEAVE_GON_BULK": "1",
    "STACKWEAVE_GON_FRESH": "1",
    "STACKWEAVE_GON_TIMING": "1",     # the bulk builder's only observable
}


def _run(body, env_extra, timeout=120):
    env = dict.fromkeys(("STACKWEAVE_GON_BULK", "STACKWEAVE_GON_FRESH",
                         "STACKWEAVE_GON_TIMING", "STACKWEAVE_STACK_ARENA",
                         "STACKWEAVE_STACK_ARENA_N"))   # None: unset unless asked
    env.update(STACKWEAVE_SYSMON="0", **env_extra)
    return run_python("import stackweave_c\n" + textwrap.dedent(body),
                      env=env, timeout=timeout)


def _assert_ran(p, marker):
    assert p.returncode == 0, (
        "worker crashed (rc=%d)\nstdout=%s\nstderr=%s"
        % (p.returncode, p.stdout[-1500:], p.stderr[-1500:]))
    assert marker in p.stdout, (
        "worker did not reach %r\nstdout=%s\nstderr=%s"
        % (marker, p.stdout[-1500:], p.stderr[-1500:]))


def _bulk_batches(p):
    return p.stderr.count("[GON_TIMING]")


_LARGE = """
    N = 20000
    stackweave_c.mn_init(8)
    try:
        def worker():
            pass
        prev = 0
        for _ in range(3):                      # stress batch reset/reuse
            stackweave_c.fiber_n(worker, N)
            done = stackweave_c.mn_run()           # cumulative completed count
            assert done - prev == N, (done - prev, N)
            prev = done
    finally:
        stackweave_c.mn_fini()
    print("LARGE_OK")
"""

_SMALL = """
    # Small batches must also drain fully (the arena lazy-inits its size class here).
    stackweave_c.mn_init(4)
    try:
        stackweave_c.fiber_n(lambda: None, 100)
        assert stackweave_c.mn_run() == 100
    finally:
        stackweave_c.mn_fini()
    print("SMALL_OK")
"""


def test_gon_bulk_takes_bulk_path_under_migration():
    """With GON_BULK=1, fiber_n(N) runs every fiber AND builds them with the
    bulk builder (one batch).  Before each bulk g got its own thread state,
    GON_BULK was ignored here: hub_main would have skipped every bulk g."""
    p = _run("""
        N = 2000
        stackweave_c.mn_init(4)
        try:
            stackweave_c.fiber_n(lambda: None, N)
            assert stackweave_c.mn_run() == N
        finally:
            stackweave_c.mn_fini()
        print("BULK_RAN_OK")
    """, BULK_ENV)
    _assert_ran(p, "BULK_RAN_OK")
    assert _bulk_batches(p) == 1, (
        "fiber_n did not take the bulk path:\n%s" % p.stderr[-800:])


def test_gon_bulk_repeated_batches_in_one_run():
    """Three fiber_n calls before one mn_run: each builds its own batch, and
    every fiber of all three runs."""
    p = _run("""
        stackweave_c.mn_init(4)
        try:
            for _ in range(3):
                stackweave_c.fiber_n(lambda: None, 500)
            assert stackweave_c.mn_run() == 1500
        finally:
            stackweave_c.mn_fini()
        print("REPEATED_OK")
    """, BULK_ENV)
    _assert_ran(p, "REPEATED_OK")
    assert _bulk_batches(p) == 3, p.stderr[-800:]


def test_bulk_fibers_are_registered_while_parked():
    """A bulk g is linked in the fiber registry before it runs and stays there
    while it is parked: fibers() reports every one in chan-wait, and a
    collection walks them (the parked-frame GC anchor walks the registry).
    mn_fini unlinks the batch's arena before freeing it, so the registry's
    struct count falls back."""
    p = _run("""
        import gc
        N = 300
        before = stackweave_c.stats()["g_structs_total"]
        stackweave_c.mn_init(4)
        try:
            ch = stackweave_c.Chan(0)
            seen = {}
            def worker(i):
                ch.recv()
            def controller():
                # Always release the workers, so a failed check below fails
                # the run at once instead of leaving N fibers parked.
                try:
                    for _ in range(2000):
                        seen["parked"] = sum(1 for f in stackweave_c.fibers()
                                             if f.get("state") == "chan-wait")
                        if seen["parked"] == N:
                            break
                        stackweave_c.sched_sleep(0.001)
                    seen["structs"] = stackweave_c.stats()["g_structs_total"] - before
                    gc.collect()
                finally:
                    for _ in range(N):
                        ch.send(None)
            stackweave_c.fiber_n(worker, N, 0, True)
            stackweave_c.mn_fiber(controller)
            assert stackweave_c.mn_run() == N + 1
        finally:
            stackweave_c.mn_fini()
        assert seen.get("parked") == N, (
            "registry shows %r/%d parked bulk fibers" % (seen.get("parked"), N))
        assert seen["structs"] >= N, seen
        left = stackweave_c.stats()["g_structs_total"] - before
        assert left < N, ("mn_fini left %d bulk g structs registered" % left)
        print("REGISTERED_OK")
    """, BULK_ENV)
    _assert_ran(p, "REGISTERED_OK")
    assert _bulk_batches(p) == 1, p.stderr[-800:]


def test_bulk_batch_dropped_at_fini_is_freed():
    """mn_fini with a bulk batch still asleep drops every g as a leftover, and
    the last drop retires the batch.  mn_fini must free that batch's arena and
    unlink it from the fiber registry (it once drained the arena quarantine
    before dropping the leftovers, so such a batch stayed allocated and
    registered)."""
    p = _run("""
        import time
        N = 400
        before = stackweave_c.stats()["g_structs_total"]
        stackweave_c.mn_init(4)
        try:
            stackweave_c.fiber_n(lambda: stackweave_c.sched_sleep(30), N)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                asleep = sum(1 for f in stackweave_c.fibers()
                             if f.get("state") == "sleep")
                if asleep == N:
                    break
                time.sleep(0.01)
            assert asleep == N, ("only %d/%d bulk fibers asleep" % (asleep, N))
        finally:
            stackweave_c.mn_fini()
        left = stackweave_c.stats()["g_structs_total"] - before
        assert left == 0, ("mn_fini left %d bulk g structs registered" % left)
        print("DROPPED_OK")
    """, BULK_ENV)
    _assert_ran(p, "DROPPED_OK")
    assert _bulk_batches(p) == 1, p.stderr[-800:]


def test_bulk_tstate_failure_unwinds_the_batch():
    """A thread state that cannot be made partway through a batch (the 50th of
    100, via the SPAWN_TSTATE fault hook) unwinds the whole batch: fiber_n
    raises MemoryError, no bulk g is registered or runs, and the batch's stack
    slots go back to the arena.  The arena holds 150 slots, so the next
    100-fiber batch can only take the bulk path if the first one's came back."""
    p = _run("""
        N = 100
        stackweave_c.mn_init(4)
        try:
            try:
                stackweave_c.fiber_n(lambda: None, N)
            except MemoryError:
                pass
            else:
                raise AssertionError("fiber_n did not raise")
            assert stackweave_c._fault_count("SPAWN_TSTATE") == 1
            assert stackweave_c.fiber_count() == 0, stackweave_c.fiber_count()
            stackweave_c.fiber_n(lambda: None, N)
            assert stackweave_c.mn_run() == N
        finally:
            stackweave_c.mn_fini()
        print("UNWIND_OK")
    """, dict(BULK_ENV, STACKWEAVE_STACK_ARENA_N="150",
              STACKWEAVE_FAULT_SPAWN_TSTATE="nth:50:12"))
    _assert_ran(p, "UNWIND_OK")
    assert _bulk_batches(p) == 1, p.stderr[-800:]


def test_bulk_large_n_lifecycle_correct():
    p = _run(_LARGE, BULK_ENV)
    _assert_ran(p, "LARGE_OK")
    assert _bulk_batches(p) == 3, p.stderr[-800:]


def test_bulk_small_n_still_correct():
    p = _run(_SMALL, BULK_ENV)
    _assert_ran(p, "SMALL_OK")
    assert _bulk_batches(p) == 1, p.stderr[-800:]


def test_stack_arena_fiber_n_lifecycle():
    """STACKWEAVE_STACK_ARENA=1 alone: every fiber_n fiber carves its stack from
    the per-size arena and returns the slot on completion, and the cursor
    resets between rounds.  Works under migration (the arena is only a stack
    allocator); runs through fiber_n's per-fiber loop."""
    p = _run(_LARGE, {"STACKWEAVE_STACK_ARENA": "1"})
    _assert_ran(p, "LARGE_OK")
    p = _run(_SMALL, {"STACKWEAVE_STACK_ARENA": "1"})
    _assert_ran(p, "SMALL_OK")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
