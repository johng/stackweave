"""Idle CPU as an invariant: a stale LEVEL arm must never busy-spin a hub.

epoll arms are LEVEL-triggered with no EPOLLET/EPOLLONESHOT, so a fd that
stays ready is re-reported on EVERY epoll_wait. If nothing is parked on it
the pump has nobody to dispatch to, and unless the arm is dropped the idle
loop re-pumps instantly and burns a whole core -- forever, if the readiness
is one that no read or write can clear.

This was found the expensive way. soupchan on ovh1, 2026-08-21: three fds
left armed for IN after their peers hung up, 1,337,592 epoll_wait calls in
10 seconds, eight hub threads at ~85% CPU each, load average 8.09 on 8
cores, 6d1h of CPU burned in 48h of wall clock. The WRITE direction had
been found and fixed before (runloom_netpoll_disarm_out, and
repro_out_busyspin.py); READ had no equivalent and no test.

So this file does not test one bug, it sweeps the CLASS: for every way a
park can leave an arm behind, assert the runtime goes quiet afterwards.
The oracle is CPU-vs-wall over an idle window, which is the only thing
that separates "parked correctly" from "spinning" -- both look identical
to a functional assertion, which is precisely why the READ case survived a
suite this large.

Two details are load-bearing, and both were learned by getting them wrong:

  1. A second fiber must stay netpoll-parked for the whole window. With
     nothing parked the scheduler never pumps netpoll at all, the stale arm
     is never observed, and every test here passes vacuously.

  2. Every case runs in a FRESH SUBPROCESS. The arm cache is process-global
     and is cleared only by netpoll_unregister; a socket dropped to the GC
     is closed by CPython's dealloc, which never reaches that hook. The
     cache then still claims "armed" for an fd NUMBER the kernel has
     forgotten, so the next case handed that number takes register's
     zero-syscall already-armed skip, never re-ADDs, and cannot spin no
     matter how broken the runtime is. In-process, these tests passed and
     failed depending on execution ORDER against a knowingly-broken build.
     Subprocess isolation is what makes the result mean something -- the
     same reason test_mn.py runs its workloads out-of-process.
"""
import os

import pytest

import stackweave_c as rc
from adv_util import needs_free_threading
from test_mn import run_mn

FT = needs_free_threading()

pytestmark = pytest.mark.skipif(rc.netpoll_backend() != "epoll",
                                reason="LEVEL-arm behaviour is epoll-specific")

# Idle window. Long enough that scheduler warmup is noise, short enough to
# keep the file quick -- a spin pegs the core from the first millisecond, so
# the signal is ~1.0 vs ~0.0 and needs no precision.
IDLE_SECONDS = 1.5
# A spin measures ~1.0 per spinning hub; correct behaviour measures ~0.00.
# Two orders of magnitude of headroom, so this threshold is not doing subtle
# work and will not go flaky on a loaded CI box.
MAX_CPU_RATIO = 0.30

_TEMPLATE = """
import os, socket, stackweave, stackweave_c
READ, WRITE = 1, 2
IDLE = {idle!r}
keep = []
res = {{}}

def cpu():
    t = os.times()
    return t.elapsed, t.user + t.system

def armed_pair(direction):
    a, b = socket.socketpair()
    a.setblocking(False); b.setblocking(False)
    keep.extend((a, b))
    if direction == READ:
        b.send(b"x")
        stackweave_c.wait_fd(a.fileno(), READ, 2000)
        a.recv(64)
    else:
        stackweave_c.wait_fd(a.fileno(), WRITE, 2000)
    return a, b

def setup():
{setup}

def measure():
    # The parked fiber that makes the pump run at all.
    c, d = socket.socketpair()
    c.setblocking(False); d.setblocking(False)
    keep.extend((c, d))
    e0, c0 = cpu()
    stackweave_c.wait_fd(c.fileno(), READ, int(IDLE * 1000))
    e1, c1 = cpu()
    res["wall"] = e1 - e0
    res["cpu"] = c1 - c0

{driver}

wall, used = res["wall"], res["cpu"]
ratio = used / wall if wall > 0 else 0.0
print("idle wall=%.2fs cpu=%.2fs ratio=%.2f" % (wall, used, ratio))
assert wall > 0, "measurement window did not run"
assert ratio < {threshold!r}, "busy-spin on a stale arm: ratio=%.2f" % ratio
print("PASS")
"""

_DRIVER_SINGLE = """
def main():
    setup()
    measure()
stackweave.run(1, main)
"""

_DRIVER_MN = """
stackweave_c.mn_init(4)
stackweave_c.mn_fiber(setup)
stackweave_c.mn_fiber(measure)
stackweave_c.mn_run()
stackweave_c.mn_fini()
"""


def _assert_quiet(setup_body, mn=False, timeout=90):
    """Run one stale-arm scenario out-of-process and require an idle runtime."""
    code = _TEMPLATE.format(
        idle=IDLE_SECONDS,
        threshold=MAX_CPU_RATIO,
        setup="\n".join("    " + line for line in setup_body.strip().splitlines()),
        driver=_DRIVER_MN if mn else _DRIVER_SINGLE,
    )
    rc_, out, err = run_mn(code, timeout=timeout)
    assert rc_ == 0 and "PASS" in out, (
        "rc={0}\n--- stdout ---\n{1}\n--- stderr ---\n{2}".format(rc_, out, err))


def test_stale_write_arm_does_not_spin():
    """The original disarm_out bug. Guards against regressing that fix."""
    _assert_quiet("armed_pair(WRITE)")


def test_stale_read_arm_with_unread_data_does_not_spin():
    """READ armed, data left in the buffer, nobody parked. Recovers on its
    own once somebody reads -- but spins until then."""
    _assert_quiet("""
a, b = armed_pair(READ)
b.send(b"leftover")
""")


def test_stale_read_arm_after_peer_hangup_does_not_spin():
    """The soupchan case, and the one that never recovers:
    EPOLLIN|EPOLLHUP|EPOLLRDHUP is asserted permanently once the peer is
    gone, so no future read can clear the level. `a` is deliberately left
    OPEN -- closing it would let the kernel drop the registration by
    itself, which is the assumption netpoll_unregister relies on and
    exactly the assumption that fails when an fd leaks."""
    _assert_quiet("""
a, b = armed_pair(READ)
b.close()
""")


def test_both_directions_armed_then_hangup_does_not_spin():
    """A bare HUP folds into BOTH direction bits in the pump, so this
    exercises the disarm_out-then-disarm_in ordering: WRITE narrows the
    kernel set to IN-only, then READ finds nothing left armed and DELs the
    registration outright."""
    _assert_quiet("""
a, b = socket.socketpair()
a.setblocking(False); b.setblocking(False)
keep.extend((a, b))
b.send(b"x")
stackweave_c.wait_fd(a.fileno(), READ, 2000)
stackweave_c.wait_fd(a.fileno(), WRITE, 2000)
a.recv(64)
b.close()
""")


def test_many_stale_arms_do_not_spin():
    """Production had three at once and they accrued over days. One is
    enough to peg a core, so this mainly guards a fix that handles a single
    fd and falls over on a set."""
    _assert_quiet("""
for _ in range(16):
    a, b = armed_pair(READ)
    b.close()
""")


def test_shutdown_write_half_close_does_not_spin():
    """Half-close rather than full close: the peer shuts down its write
    side, so `a` sees EOF with the socket still open at both ends. A
    reverse proxy draining a keep-alive connection produces exactly this,
    which is the shape soupchan sat behind."""
    _assert_quiet("""
a, b = armed_pair(READ)
b.shutdown(socket.SHUT_WR)
""")


def test_listener_with_pending_connection_does_not_spin():
    """A listening socket armed for READ with a connection still queued and
    no acceptor parked. An accept loop between iterations is exactly this,
    and it is the highest-traffic arm in any server."""
    _assert_quiet("""
lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
lst.bind(("127.0.0.1", 0)); lst.listen(8); lst.setblocking(False)
keep.append(lst)
cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
cli.connect(lst.getsockname())
keep.append(cli)
stackweave_c.wait_fd(lst.fileno(), READ, 2000)
conn, _ = lst.accept()
keep.append(conn)
cli2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
cli2.connect(lst.getsockname())
keep.append(cli2)
""")


@pytest.mark.skipif(not FT, reason="M:N needs a free-threaded build")
def test_stale_read_arm_does_not_spin_under_mn():
    """The same thing with real hubs, which is how production ran it.

    This is the case that actually cost the box: all eight hubs waited on
    the SAME shared epoll, so one permanently-ready fd did not spin one
    hub, it spun every one of them. A single-hub test understates the bug
    by the hub count."""
    _assert_quiet("""
a, b = armed_pair(READ)
b.close()
""", mn=True)
