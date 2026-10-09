"""A run(1) nested in an M:N fiber runs its fibers like run(1) anywhere else.

run(1) drains this thread's single-thread scheduler, and stackweave's asyncio
loop drives the same drain, so both can run on a hub's fiber.  Every sleep,
park, yield and preemption checked "is this thread a hub running a fiber?"
first, and on a hub's fiber that was true even inside the nested drain.  So an
inner fiber that slept parked the HUB's fiber -- still running, its stack
hosting the drain -- while only the inner coroutine was swapped out: the inner
fiber was never resumed and run(1) returned without it, and the hub's fiber
sat on a sleep heap or run queue to be resumed again later.  The per-g
snapshot skip keyed on the same check, so inner fibers shared one frame chain
and exception state.  A Coro resumed on a hub's fiber had the same exposure to
a preemption.

While a nested drain or Coro runs, the hub is now hidden from the code below
it (runloom_mn_nested_here), which takes the single-thread paths.

Each case runs in its own process under run(2), with busy sibling fibers so
the hubs have other work and nothing takes a fast path.
"""
import sys
import textwrap

import pytest

from adv_util import run_python


PRELUDE = textwrap.dedent("""
    import faulthandler, os, socket, sys, time
    import stackweave, stackweave_c as rc
    faulthandler.dump_traceback_later(60, exit=True)

    def busy():
        for _ in range(300):
            rc.sched_yield()

    def under_mn(outer, busy_fibers=4):
        # outer runs on an M:N fiber, next to busy siblings.
        def root():
            stackweave.fiber(outer)
            for _ in range(busy_fibers):
                stackweave.fiber(busy)
        stackweave.run(2, root)
""")


def _run(body, timeout=120):
    p = run_python(PRELUDE + textwrap.dedent(body), timeout=timeout)
    assert p.returncode == 0, (p.returncode, p.stdout[-2000:] + p.stderr[-3000:])
    return p.stdout + p.stderr


def test_inner_fibers_that_sleep_all_finish():
    out = _run("""
        res = {}
        def outer():
            done = []
            def sleeper(i):
                rc.sched_sleep(0.005 * (i % 3))
                done.append(i)
            def main():
                for i in range(6):
                    rc.fiber(lambda i=i: sleeper(i))
            res["n"] = stackweave.run(1, main)
            res["done"] = sorted(done)
        under_mn(outer)
        print("N", res.get("n"), "DONE", res.get("done"))
    """)
    assert "N 7 DONE [0, 1, 2, 3, 4, 5]" in out, out


def test_inner_fibers_interleave_on_yield_with_their_own_frames():
    # Two inner fibers yield back and forth from inside nested calls and an
    # except block: each must come back to its own frames, locals and
    # exception, in turn.
    out = _run("""
        res = {}
        def outer():
            order = []
            def worker(tag, n):
                def deep(k):
                    if k == 0:
                        try:
                            raise ValueError(tag)
                        except ValueError:
                            for i in range(n):
                                order.append((tag, i))
                                rc.sched_yield()
                                assert sys.exc_info()[1].args == (tag,), sys.exc_info()
                            return tag * 2
                    return deep(k - 1)
                res[tag] = deep(5)
            def main():
                rc.fiber(lambda: worker("a", 3))
                rc.fiber(lambda: worker("b", 3))
            res["n"] = stackweave.run(1, main)
            res["order"] = order
        under_mn(outer)
        print("N", res.get("n"), "A", res.get("a"), "B", res.get("b"))
        print("ORDER", res.get("order"))
    """)
    assert "N 3 A aa B bb" in out, out
    assert "ORDER [('a', 0), ('b', 0), ('a', 1), ('b', 1), ('a', 2), ('b', 2)]" in out, out


def test_inner_fibers_park_on_a_channel():
    out = _run("""
        res = {}
        def outer():
            got = []
            ch = rc.Chan(0)
            def producer():
                for i in range(20):
                    ch.send(i)
                ch.close()
            def consumer():
                while True:
                    v, ok = ch.recv()
                    if not ok:
                        return
                    got.append(v)
            def main():
                rc.fiber(consumer)
                rc.fiber(producer)
            res["n"] = stackweave.run(1, main)
            res["got"] = got
        under_mn(outer)
        print("N", res.get("n"), "GOT", res.get("got") == list(range(20)))
    """)
    assert "N 3 GOT True" in out, out


def test_inner_fiber_waits_on_a_socket():
    out = _run("""
        res = {}
        def outer():
            a, b = socket.socketpair()
            a.setblocking(False); b.setblocking(False)
            def reader():
                res["ready"] = rc.wait_fd(a.fileno(), 1, 5000)
                res["data"] = a.recv(16)
            def writer():
                rc.sched_sleep(0.01)
                b.send(b"ping")
            def main():
                rc.fiber(reader)
                rc.fiber(writer)
            res["n"] = stackweave.run(1, main)
            for s in (a, b):
                rc.netpoll_unregister(s.fileno())
                s.close()
        under_mn(outer)
        print("N", res.get("n"), "READY", res.get("ready"), "DATA", res.get("data"))
    """)
    assert "N 3 READY 1 DATA b'ping'" in out, out


def test_the_outer_fiber_gets_its_hub_back():
    out = _run("""
        res = {}
        def outer():
            res["hub before"] = rc.mn_current_hub() is not None
            def main():
                res["hub inside"] = rc.mn_current_hub()
                rc.sched_sleep(0.005)
            stackweave.run(1, main)
            res["hub after"] = rc.mn_current_hub() is not None
            t0 = time.monotonic()
            rc.sched_sleep(0.02)          # an M:N sleep again
            res["slept"] = time.monotonic() - t0 >= 0.015
        under_mn(outer)
        print("RES", res["hub before"], res["hub inside"], res["hub after"], res["slept"])
    """)
    assert "RES True None True True" in out, out


def test_a_long_coro_body_in_an_mn_fiber_is_not_preempted_out_of():
    # The sysmon preempts a hub's fiber that runs past its slice.  Inside a
    # Coro on that fiber it used to queue the hub's fiber and swap out the
    # Coro instead, so resume() came back early and the fiber ran twice.
    out = _run("""
        res = {}
        def outer():
            def body():
                end = time.monotonic() + 0.3
                n = 0
                while time.monotonic() < end:
                    n += 1
                return n
            c = rc.Coro(body)
            c.resume()
            res["done"] = c.done
            rc.sched_sleep(0.01)
            res["after"] = True
        under_mn(outer, busy_fibers=8)
        print("DONE", res.get("done"), "AFTER", res.get("after"))
    """)
    assert "DONE True AFTER True" in out, out


def test_a_coro_body_that_calls_python_is_not_preempted_out_of():
    # Same as above, but each iteration enters a Python frame, so the sysmon
    # eval-frame hook gets its chance (the loop above only meets the
    # liveness pending call).
    out = _run("""
        res = {}
        def step(n):
            return n + 1
        def outer():
            def body():
                end = time.monotonic() + 0.3
                n = 0
                while time.monotonic() < end:
                    n = step(n)
                return n
            c = rc.Coro(body)
            c.resume()
            res["done"] = c.done
        under_mn(outer, busy_fibers=8)
        print("DONE", res.get("done"))
    """)
    assert "DONE True" in out, out


def test_gather_inside_a_nested_run1_waits_for_its_runners():
    # gather() used to send its runners to the hubs (mn_hub_count() > 0) and
    # park its waiter in the nest, which did not count that park: run(1)
    # returned 0 with the gather unfinished.  Spawns made in a nest now stay
    # in it.
    out = _run("""
        from stackweave.sync import gather
        res = {}
        def outer():
            def main():
                res["gather"] = gather(lambda: rc.sched_sleep(0.01) or 1,
                                       lambda: 2, lambda: 3)
            res["n"] = stackweave.run(1, main)
        under_mn(outer)
        print("N", res.get("n"), "GATHER", res.get("gather"))
    """)
    assert "N 4 GATHER [1, 2, 3]" in out, out


def test_spawns_stay_in_a_nested_run1_and_go_to_the_hubs_outside_it():
    out = _run("""
        res = {}
        def outer():
            res["outside"] = rc.mn_spawns_to_hubs()
            def main():
                res["inside"] = rc.mn_spawns_to_hubs()
                c = rc.Coro(lambda: res.setdefault("coro in run(1)", rc.mn_spawns_to_hubs()))
                c.resume()
            stackweave.run(1, main)
            c = rc.Coro(lambda: res.setdefault("coro", rc.mn_spawns_to_hubs()))
            c.resume()
            res["after"] = rc.mn_spawns_to_hubs()
        under_mn(outer, busy_fibers=0)
        print("RES", res["outside"], res["inside"], res["coro in run(1)"],
              res["coro"], res["after"])
    """)
    # A Coro body has no drain of its own, so outside a run(1) its spawns
    # still go to the hubs, where they run.
    assert "RES True False False True True" in out, out


def test_asyncio_on_the_stackweave_loop_inside_an_mn_fiber():
    out = _run("""
        import asyncio
        from stackweave import aio
        res = {}
        async def amain():
            async def t(i):
                await asyncio.sleep(0.005 * (i % 3))
                return i
            r = await asyncio.gather(*(t(i) for i in range(6)))
            q = asyncio.Queue()
            async def prod():
                for i in range(10):
                    await q.put(i)
            async def cons():
                return [await q.get() for _ in range(10)]
            _, got = await asyncio.gather(prod(), cons())
            return r, got
        def outer():
            res["r"] = aio.run(amain())
            res["hub after"] = rc.mn_current_hub() is not None
        under_mn(outer)
        print("R", res.get("r"), "HUB", res.get("hub after"))
    """)
    assert ("R ([0, 1, 2, 3, 4, 5], [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]) HUB True"
            in out), out


LEFTOVER = """
    res, hold = {}, {}
    def leftover():
        # Parks inside an except block, so its snapshot holds the exc_info
        # chain of the thread state it ran on: the nesting fiber's.
        try:
            raise ValueError("from-A")
        except ValueError:
            hold["g"] = rc.current_g()
            rc.park()                # not counted: run(1) returns without it
            res["exc"] = repr(sys.exc_info()[1])
    def b():
        res["b ran"] = stackweave.run(1)   # drains whatever "this thread's" sched has
"""


def test_a_fiber_left_in_a_nested_run1_stays_with_the_fiber_that_ran_it():
    # A's run(1) returns with `leftover` parked; A wakes it.  B, pinned to the
    # same hub thread, then runs run(1): it must not resume A's fiber, which
    # would run on B's thread state with A's exception chain.  A's own next
    # run(1) resumes it, with its exception intact.
    out = _run(LEFTOVER + """
    def a():
        hub = rc.mn_current_hub()
        res["a first"] = stackweave.run(1, lambda: rc.fiber(leftover))
        hold["g"].wake()
        rc.mn_fiber(b, 0, hub)
        rc.sched_sleep(0.05)          # B runs on A's hub meanwhile
        res["a again"] = stackweave.run(1)
    under_mn(a, busy_fibers=0)
    print("RES", res["a first"], res["b ran"], res["a again"], res.get("exc"))
    """)
    assert "RES 1 0 1 ValueError('from-A')" in out, out


@pytest.mark.parametrize("woken", [False, True])
def test_a_fiber_left_behind_by_an_ended_fiber_is_never_resumed(woken):
    # As above, but A ends without another run(1), freeing its thread state,
    # before B's run(1).  Resuming the leftover there was a use-after-free.
    # Woken (queued to run), it is dropped when A ends, as sched_reset drops
    # queued fibers; still parked, it may yet be woken by whoever holds it, so
    # it is kept -- never resumed -- and reported.
    out = _run(LEFTOVER + """
    def a():
        hub = rc.mn_current_hub()
        res["a first"] = stackweave.run(1, lambda: rc.fiber(leftover))
        if WOKEN:
            hold["g"].wake()
        def later():
            rc.sched_sleep(0.05)      # after A has ended
            b()
        rc.mn_fiber(later, 0, hub)
    under_mn(a, busy_fibers=0)
    print("RES", res["a first"], res.get("b ran"), res.get("exc"))
    """.replace("WOKEN", repr(woken)))
    assert "RES 1 0 None" in out, out
    assert ("can no longer run" in out) == (not woken), out


def test_aio_leftovers_of_a_loop_closed_in_an_mn_fiber_are_dropped():
    # loop.close() -> sched_reset() drops what the loop left on the fiber's
    # own scheduler -- an add_reader watcher parked in wait_fd, a server's
    # accept fiber -- as on a plain thread, so nothing is left when the fiber
    # ends ("can no longer run") and a second sched_reset finds nothing.
    out = _run("""
        import asyncio
        from stackweave import aio
        res = {}
        a, b = socket.socketpair()
        async def watcher():
            asyncio.get_running_loop().add_reader(a.fileno(), lambda: None)
            await asyncio.sleep(0.01)          # never removed
        async def server():
            srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
            await asyncio.sleep(0.01)          # never closed
        def outer():
            aio.run(watcher())
            res["after watcher"] = rc.sched_reset()
            aio.run(server())
            res["after server"] = rc.sched_reset()
        under_mn(outer, busy_fibers=0)
        print("RES", res["after watcher"], res["after server"])
    """)
    assert "RES (0, 0, 0) (0, 0, 0)" in out, out
    assert "can no longer run" not in out, out


def test_loops_closed_while_another_fibers_loop_is_open_leave_nothing():
    # aio skips sched_reset while any other loop is open; each fiber's own
    # scheduler then still holds the closed loop's call_later timer.  The
    # fiber's end drops it, since nothing can run it any more.
    out = _run("""
        import asyncio
        from stackweave import aio
        async def long_main():
            await asyncio.sleep(0.3)
        async def small():
            asyncio.get_running_loop().call_later(30, lambda: None)
            await asyncio.sleep(0)
        def root():
            stackweave.fiber(lambda: aio.run(long_main()))
            rc.sched_sleep(0.05)
            for _ in range(20):
                stackweave.fiber(lambda: aio.run(small()))
        stackweave.run(2, root)
        print("DONE")
    """)
    assert "DONE" in out, out
    assert "can no longer run" not in out, out


def test_a_finished_fiber_whose_handle_outlives_its_owner_is_not_reported():
    out = _run("""
        keep = []
        def outer():
            stackweave.run(1, lambda: keep.append(stackweave.fiber(lambda: 1)))
        under_mn(outer, busy_fibers=0)
        print("DONE", [k.done for k in keep])
    """)
    assert "DONE [True]" in out, out
    assert "can no longer run" not in out, out


def test_set_stack_size_applies_to_an_mn_fibers_own_scheduler():
    out = _run("""
        sizes = []
        def measure():
            top, soft, hard = rc._c_stack_limits()
            sizes.append(abs(top - hard) >> 10)
        def outer():
            stackweave.run(1, lambda: rc.fiber(measure))
            rc.set_stack_size(4 << 20)
            stackweave.run(1, lambda: rc.fiber(measure))
        under_mn(outer, busy_fibers=0)
        print("GREW", sizes[1] > 3 * sizes[0], sizes)
    """)
    assert "GREW True" in out, out


def test_a_nest_moves_with_its_fiber_to_another_hub():
    # A leaves two fibers in its nested run(1) -- one parked inside an except
    # block, one in wait_fd -- moves to the other hub (pin, park, woken by a
    # sibling), and runs run(1) again there: both resume, on A's thread state.
    out = _run("""
        res, hold = {}, {}
        a, b = socket.socketpair(); a.setblocking(False); b.setblocking(False)
        def exc_parker():
            try:
                raise ValueError("from-A")
            except ValueError:
                hold["g"] = rc.current_g()
                rc.park()
                res["exc"] = repr(sys.exc_info()[1])
        def fd_parker():
            res["ready"] = rc.wait_fd(a.fileno(), 1, 5000)
            res["data"] = a.recv(16)
        def outer():
            def main():
                rc.fiber(exc_parker)
                rc.fiber(fd_parker)
                def stop():
                    rc.sched_sleep(0.02)
                    rc.sched_stop()       # return with fd_parker still parked
                rc.fiber(stop)
            res["hub1"] = rc.mn_current_hub()
            res["n1"] = stackweave.run(1, main)
            hold["g"].wake()
            me = rc.current_g()
            me.pin(1 - res["hub1"])
            def waker():
                while me.stack()["state"] != "parked":
                    rc.sched_yield()
                me.wake()
            stackweave.fiber(waker)
            rc.park()                     # resumes on the other hub
            res["moved"] = rc.mn_current_hub() != res["hub1"]
            b.send(b"ping")
            res["n2"] = stackweave.run(1)
            rc.netpoll_unregister(a.fileno())
        under_mn(outer, busy_fibers=2)
        print("RES", res.get("n1"), res.get("moved"), res.get("n2"),
              res.get("exc"), res.get("ready"), res.get("data"))
    """)
    assert "RES 2 True 2 ValueError('from-A') 1 b'ping'" in out, out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
