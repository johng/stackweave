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
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

from adv_util import needs_free_threading

ROOT = pathlib.Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not needs_free_threading(),
                                reason="M:N needs free-threaded CPython")

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
    p = subprocess.run([sys.executable, "-c", PRELUDE + textwrap.dedent(body)],
                       cwd=ROOT, env=dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src"),
                       capture_output=True, text=True, timeout=timeout)
    assert p.returncode == 0, (p.returncode, p.stdout[-2000:] + p.stderr[-3000:])
    return p.stdout


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
