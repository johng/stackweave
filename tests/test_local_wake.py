"""Go-style local wake under migration mode (runloom_mn_woken_enqueue).

A wake performed ON a general hub thread pushes the woken fiber onto the
waker's own Chase-Lev deque instead of the mutex-protected global run-queue.
The waker's hub picks it up at its next pick step, or an idle hub steals it.
The global queue is left for foreign-thread wakers, offload-hub wakers, pinned
fibers, controlled replay, and a full deque.

These tests pin down the soundness properties the redirect must keep --
at-most-one resume per wake, no lost wake, no hang, clean quiescence -- and,
on a -DRUNLOOM_COVER build, that the local path is actually the one taken.
Which hub a locally-woken fiber resumes on is deliberately NOT asserted: it is
the waker's hub unless an idle hub stole it first, and both are correct.

Run directly:  PYTHON_GIL=0 PYTHONPATH=src python tests/test_local_wake.py
"""
import os
import threading

# Read once at the first mn_init, so it has to be set before the runtime starts.
os.environ.setdefault("RUNLOOM_MIGRATION", "1")

import pytest

import runloom
import runloom_c as rc

from adv_util import needs_free_threading

needs_migration = pytest.mark.skipif(
    not (needs_free_threading() and runloom.migration_available()),
    reason="needs a free-threaded build carrying both CPython migration patches")

HUBS = 4


def _cover(name):
    """Hit count for a named cover point, or None when not a -DRUNLOOM_COVER build."""
    if not rc._cover_enabled():
        return None
    return rc._cover_report().get(name, 0)


def _run(*fibers, hubs=HUBS):
    if rc._cover_enabled():
        rc._cover_reset()
    rc.mn_init(hubs)
    for f in fibers:
        rc.mn_fiber(f)
    rc.mn_run()
    stats = rc.stats()
    rc.mn_fini()
    return stats


@needs_migration
def test_cross_hub_ping_pong_completes_and_takes_the_local_path():
    """Two fibers volley over a pair of channels.  Every recv parks and every
    send is a wake performed on a hub thread -> the local-wake path.  Both must
    finish, the count must be exact, and the runtime must be quiescent after."""
    rounds = 5000
    a2b, b2a, out = rc.Chan(0), rc.Chan(0), rc.Chan(2)

    def ping():
        n = 0
        for i in range(rounds):
            a2b.send(i)
            v, _ = b2a.recv()
            assert v == i
            n += 1
        out.send(("ping", n))

    def pong():
        n = 0
        for _ in range(rounds):
            v, _ = a2b.recv()
            b2a.send(v)
            n += 1
        out.send(("pong", n))

    stats = _run(ping, pong)
    got = dict(out.try_recv()[0] for _ in range(2))
    assert got == {"ping": rounds, "pong": rounds}
    assert stats["mn_pending_total"] == 0
    hits = _cover("local_wake")
    if hits is not None:
        # Each of the rounds*2 handoffs is a hub-thread wake of an unpinned g.
        assert hits > 0, "hub-thread wakes still went through the global run-queue"


@needs_migration
def test_wake_burst_from_one_hub_resumes_each_fiber_exactly_once():
    """N fibers park; one waker on a hub thread wakes them all back-to-back.
    Local wake puts them all on the waker's deque -- more than the waker can
    run before the others steal.  Every fiber must resume exactly once and
    the run must reach quiescence (no lost wake, no duplicate entry)."""
    n = 256
    handoff, done = rc.Chan(n), rc.Chan(n)

    def sleeper(i):
        def body():
            handoff.send(rc.current_g())
            rc.park()
            done.send((i, rc.mn_current_hub()))
        return body

    def waker():
        gs = [handoff.recv()[0] for _ in range(n)]
        for g in gs:
            while g.stack()["state"] != "parked":
                rc.yield_()
        for g in gs:
            g.wake()

    stats = _run(waker, *[sleeper(i) for i in range(n)])
    seen = {}
    for _ in range(n):
        i, hub = done.try_recv()[0]
        assert i not in seen, f"fiber {i} resumed twice"
        seen[i] = hub
    assert len(seen) == n
    assert stats["mn_pending_total"] == 0


@needs_migration
def test_foreign_thread_wake_still_falls_back_to_the_global_queue():
    """A waker that is not a hub thread has no deque of its own: the wake must
    take the global run-queue path and still be delivered."""
    handoff, done = rc.Chan(1), rc.Chan(1)

    def sleeper():
        handoff.send(rc.current_g())
        rc.park()
        done.send("woke")

    def kicker():
        g, _ = handoff.recv()
        while g.stack()["state"] != "parked":
            rc.yield_()
        t = threading.Thread(target=g.wake)
        t.start()
        t.join()

    stats = _run(sleeper, kicker)
    assert done.try_recv()[0] == "woke"
    assert stats["mn_pending_total"] == 0
    pulls = _cover("global_runq_pull")
    if pulls is not None:
        assert pulls >= 1, "foreign-thread wake bypassed the global run-queue"


@needs_migration
def test_pinned_fiber_is_not_placed_on_the_waker_deque():
    """A pinned fiber may only run on its pinned hub; a deque is stealable by
    any general hub, so the wake must go global and honour the pin.  Mirrors
    test_hub_pinning's contract from the local-wake side."""
    handoff, res = rc.Chan(1), rc.Chan(1)

    def fiber():
        g = rc.current_g()
        g.pin(2)
        handoff.send(g)
        rc.park()
        res.send(rc.mn_current_hub())

    def waker():
        g, _ = handoff.recv()
        while g.stack()["state"] != "parked":
            rc.yield_()
        g.wake()

    rc.mn_init(HUBS)
    rc.mn_fiber(fiber, hub=0)
    rc.mn_fiber(waker, hub=1)
    rc.mn_run()
    hub, _ = res.try_recv()
    rc.mn_fini()
    assert hub == 2


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
