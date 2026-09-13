"""Minimal working example: a fiber spawns on hub 0, then resumes where it says.

    mn_fiber(fn, hub=0)   spawn on hub 0 and stay there (no work-stealing)
    G.pin(N)              confine the next resume to hub N

Unpinned, this workload resumes on hub 0 every time, so pin(1)/(2)/(3) each
demand an outcome the free scheduler does not produce and pin(0) is the
stayed-put control.  A pin degraded to a no-op fails three of the four.

Run directly:  PYTHON_GIL=0 PYTHONPATH=src python tests/test_hub_pinning.py
"""
import os
import time

# Read once at the first mn_init, so it has to be set before the runtime starts.
os.environ.setdefault("RUNLOOM_MIGRATION", "1")

import pytest

import runloom
import runloom_c as rc

from adv_util import needs_free_threading

HUBS = 4


def spawn_on_0_resume_on(hub):
    handoff, res = rc.Chan(1), rc.Chan(2)

    def fiber():
        g = rc.current_g()
        g.pin(hub)                        # next resume must be on this hub
        handoff.send(g)
        res.send(rc.mn_current_hub())     # spawned on hub 0
        rc.park()
        res.send(rc.mn_current_hub())     # resumed on `hub`

    def waker():
        g, _ = handoff.recv()
        # Wait until it has genuinely suspended: park_safe/wake_safe absorbs an
        # early wake, and a fiber that never suspends never migrates.
        while g.stack()["state"] != "parked":
            rc.yield_()
        g.wake()                          # ordinary wake; the pin picked the hub

    rc.mn_init(HUBS)
    rc.mn_fiber(fiber, hub=0)
    rc.mn_fiber(waker, hub=1)
    rc.mn_run()
    before, _ = res.try_recv()
    after, _ = res.try_recv()
    rc.mn_fini()
    return before, after


@pytest.mark.skipif(not needs_free_threading(), reason="needs a free-threaded build")
@pytest.mark.skipif(not runloom.migration_available(),
                    reason="needs both CPython migration patches (src/patches/)")
@pytest.mark.parametrize("hub", range(HUBS))   # 0 stays put; 1-3 must move
def test_fiber_resumes_on_the_hub_it_pinned_itself_to(hub):
    assert spawn_on_0_resume_on(hub) == (0, hub)


@pytest.mark.skipif(not needs_free_threading(), reason="needs a free-threaded build")
def test_pin_on_a_handle_from_a_torn_down_session_is_refused():
    """A G handle can outlive its M:N session, and g->park_hub then points into
    the hub array mn_fini freed.  pin() must refuse on a generation mismatch, as
    wake() does, rather than dereference it.  Needs no migration patches: the
    dereference is on the default-scheduler path."""
    esc = []

    def sess1():
        ch = rc.Chan(1)

        def fiber():
            g = rc.current_g()
            esc.append(g)
            ch.send(g)
            rc.park()

        def waker():
            g, _ = ch.recv()
            while g.stack()["state"] != "parked":
                rc.yield_()
            g.wake()

        rc.mn_init(4)
        rc.mn_fiber(fiber, hub=0)
        rc.mn_fiber(waker, hub=1)
        rc.mn_run()
        rc.mn_fini()

    sess1()
    junk = [bytearray(b"\x41" * 8192) for _ in range(2000)]   # poison the freed block
    rc.mn_init(1)
    rc.mn_fiber(lambda: None)
    rc.mn_run()
    try:
        with pytest.raises(RuntimeError, match="torn-down"):
            esc[0].pin(0)
    finally:
        rc.mn_fini()
        del junk


@pytest.mark.skipif(not needs_free_threading(), reason="needs a free-threaded build")
@pytest.mark.skipif(not runloom.migration_available(),
                    reason="needs both CPython migration patches (src/patches/)")
def test_pinned_runq_entry_does_not_spin_the_other_hubs():
    """A pinned global-runq entry is work for its target hub only; the idle scan
    must not keep the other hubs awake for it.  process_time() is per-process,
    so a parallel suite does not skew the measurement."""
    HUBS, BUSY = 8, 1.0
    TARGET = 3

    def body():
        ch = rc.Chan(1)

        def sleeper():
            g = rc.current_g()
            g.pin(TARGET)
            ch.send(g)
            rc.park()

        def hog():
            end = time.monotonic() + BUSY
            while time.monotonic() < end:
                pass

        def waker():
            g, _ = ch.recv()
            while g.stack()["state"] != "parked":
                rc.yield_()
            g.wake()

        rc.mn_init(HUBS)
        rc.mn_fiber(sleeper, hub=0)
        rc.mn_fiber(hog, hub=TARGET)
        rc.mn_fiber(waker, hub=1)
        rc.mn_run()
        rc.mn_fini()

    w0, c0 = time.monotonic(), time.process_time()
    body()
    wall, cpu = time.monotonic() - w0, time.process_time() - c0
    # One hub legitimately burns a core for BUSY (~1.0x); spinning idle hubs
    # measure 3.3x, so 2.0 has headroom either side.
    assert cpu / wall < 2.0, (
        "idle hubs spun on a pinned runq entry: cpu=%.2fs wall=%.2fs (%.1fx)"
        % (cpu, wall, cpu / wall))


@pytest.mark.skipif(not needs_free_threading(), reason="needs a free-threaded build")
@pytest.mark.skipif(not runloom.migration_available(),
                    reason="needs both CPython migration patches (src/patches/)")
def test_repinning_a_queued_fiber_keeps_the_runq_counters_consistent():
    """pin() must not change pin_hub1 behind the runq lock's back: the fiber is
    woken while its target hub is busy (so the entry sits queued), then
    unpinned, and the runq counters must still balance.  Asserted on the idle
    tail after everything has finished: healthy measures ~0.03 cpu/wall and
    drifted 0.22, so the bound is 0.10."""
    HUBS, TARGET, BUSY = 4, 3, 1.5

    def body():
        ch = rc.Chan(1)

        def victim():
            g = rc.current_g()
            g.pin(TARGET)
            ch.send(g)
            rc.park()

        def hog():
            end = time.monotonic() + BUSY
            while time.monotonic() < end:
                pass

        rc.mn_fiber(victim, hub=0)
        rc.mn_fiber(hog, hub=TARGET)
        g, _ = ch.recv()
        while g.stack()["state"] != "parked":
            rc.yield_()
        g.wake()                 # queued, pinned to the busy TARGET -> no pull yet
        runloom.sleep(0.3)
        g.pin(None)              # re-classify WHILE QUEUED
        runloom.sleep(BUSY + 0.5)

    rc.mn_init(HUBS)
    rc.mn_fiber(body)
    rc.mn_run()
    w0, c0 = time.monotonic(), time.process_time()
    time.sleep(1.0)              # nothing is runnable; hubs must be asleep
    wall, cpu = time.monotonic() - w0, time.process_time() - c0
    rc.mn_fini()
    assert cpu / wall < 0.10, (
        "hubs spun in an idle tail: cpu=%.2fs wall=%.2fs (%.2fx) -- runq counters "
        "drifted when a queued fiber was re-pinned" % (cpu, wall, cpu / wall))


if __name__ == "__main__":
    if runloom.migration_available():
        for hub in range(HUBS):
            print("pin(%d) -> ran on hub %d, then hub %d"
                  % ((hub,) + spawn_on_0_resume_on(hub)))
    else:
        print("skipped --", runloom.migration_status())
