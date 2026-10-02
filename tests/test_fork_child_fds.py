"""A forked child opens pollers only for the pools it uses.

Each of the RUNLOOM_PARKER_POOL_MAX parker pools (one per possible hub, plus
the default) gets its own poller -- epoll + eventfd, or kqueue + self-pipe --
on first use.  The at-fork reset closed the inherited ones and then created a
fresh poller for every slot, so every forked child of a process that had
imported stackweave started with 130 extra fds on epoll and 195 on kqueue,
whether it ran a hub or not.  Hub pools now go back to never-used in the child
and are created on first use, like in a fresh process.

Each case runs in a subprocess, which forks; the child reports back over a
pipe.
"""
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

from adv_util import needs_free_threading

ROOT = pathlib.Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork required")

PRELUDE = textwrap.dedent("""
    import os, socket, sys
    import stackweave, stackweave_c as rc

    def nfds():
        for p in ("/proc/self/fd", "/dev/fd"):
            try:
                return len(os.listdir(p))
            except OSError:
                pass
        raise SystemExit("no fd directory")

    def park_once():
        # A real park on this hub's pool: wait_fd until a sibling sends.
        a, b = socket.socketpair()
        a.setblocking(False); b.setblocking(False)
        def send():
            rc.sched_yield()
            rc.tcp_send(b.fileno(), b"x")
        rc.mn_fiber(send)
        assert rc.wait_fd(a.fileno(), 1, 5000) == 1
        buf = bytearray(1)
        rc.tcp_recv(a.fileno(), buf, 1)
        for s in (a, b):
            rc.netpoll_unregister(s.fileno())
            s.close()

    def io_on_every_hub(n):
        # How many hubs completed a park.  Counted, not asserted in the fiber:
        # an exception in a fiber is printed and dropped, and run() returns.
        done = bytearray(n)
        def on_hub(h):
            park_once()
            done[h] = 1
        def driver():
            for h in range(n):
                rc.mn_fiber(lambda h=h: on_hub(h), hub=h)
        stackweave.run(n, driver)
        return sum(done)

    def in_child(fn):
        # Run fn() in a forked child; return what it returns (an int).
        r, w = os.pipe()
        before = nfds()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            code = 1
            try:
                os.write(w, str(fn()).encode())
                code = 0
            except BaseException:
                import traceback
                traceback.print_exc()
            finally:
                os._exit(code)
        os.close(w)
        out = os.read(r, 64)
        os.close(r)
        _, status = os.waitpid(pid, 0)
        assert status == 0 and out, (status, out)
        return before, int(out)
""")


def _run(body):
    p = subprocess.run([sys.executable, "-c", PRELUDE + textwrap.dedent(body)],
                       cwd=ROOT, env=dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src"),
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr[-3000:]
    return p.stdout


def _extra(out):
    parent, child = map(int, out.split("FDS", 1)[1].split())
    return child - parent


def test_a_child_of_an_importer_inherits_no_extra_pollers():
    # The child re-creates only the default pool's poller (kqueue + self-pipe
    # = 3 fds, epoll + eventfd = 2) and closes its pipe's read end before it
    # counts: at most 2 more than the parent.  One hub pool's poller would
    # make it 3-5 more; all of them, 130-195.
    out = _run("""
        parent, child = in_child(nfds)
        print("FDS", parent, child)
    """)
    assert _extra(out) <= 2, out


@pytest.mark.skipif(not needs_free_threading(), reason="M:N needs free-threaded CPython")
def test_a_child_of_a_hub_runner_inherits_no_extra_pollers():
    out = _run("""
        assert io_on_every_hub(4) == 4     # the parent's hub pools are live
        parent, child = in_child(nfds)
        print("FDS", parent, child)
    """)
    assert _extra(out) <= 2, out


@pytest.mark.skipif(not needs_free_threading(), reason="M:N needs free-threaded CPython")
def test_a_child_creates_hub_pollers_on_first_use():
    # A hub pool the child never inherited a poller for still parks and wakes,
    # on every hub, twice: once on pools it creates, once on pools it made.
    out = _run("""
        assert io_on_every_hub(4) == 4
        _, parks = in_child(lambda: io_on_every_hub(4) + io_on_every_hub(4))
        print("PARKS", parks)
    """)
    assert "PARKS 8" in out, out
