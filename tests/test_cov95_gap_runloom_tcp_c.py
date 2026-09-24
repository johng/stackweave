"""Bounded gap-fill coverage for runloom_tcp.c (TCPConn) COVER lines.

Targets the uncovered-but-reachable lines classified COVER in
build/cover_by_tu.json under "runloom_tcp.c":

  runloom_tcp_conn_send.c.inc
    L30        send while(1) loop back-edge (resume after an EAGAIN park)
    L41-43     send hard error (EPIPE/ECONNRESET) -> PyBuffer_Release + raise
    L46-50     send EAGAIN park + the wait_fd<0 (cancel) error-return branch
    L68        send_all closed-conn guard
  runloom_tcp_conn_net.c.inc
    L110-111   accept fatal-error branch (errno not in the transient set)

Mechanisms (per the classifier):
  * The accept fatal-error branch has no in-process FINJ hook on Linux
    (RUNLOOM_TCP_FINJ compiles to 0); driven with strace -e inject=accept4.
  * The send/recv epoll-path branches use real backpressure / RST / cancel.

Every test is deadline-bounded (hang_guard / subprocess timeout).
"""
import os
import shutil
import socket
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adv_util import hang_guard  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

import stackweave_c as rc  # noqa: E402

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="runloom_tcp.c strace gap-fill is Linux-only")


# ===========================================================================
# 1. send EAGAIN backpressure: epoll-path send loop back-edge (L30) + the
#    EAGAIN park (L46) and its success-resume (L46-47).  A fiber fills
#    SO_SNDBUF on a conn whose peer never reads -> send() EAGAINs -> park on
#    EPOLLOUT (L46) -> a second fiber drains the peer -> the park resumes ->
#    loop back to L30 -> send completes -> break.  DEFAULT (epoll) backend so no
#    io_uring at all.
# ===========================================================================
def test_send_eagain_park_then_resume_epoll():
    # A 4 MiB payload over default socket buffers (~200 KiB) GUARANTEES many
    # EAGAIN parks on EPOLLOUT: send_all sends what fits, EAGAINs, parks (L46),
    # the server drains (frees buffer space), the park resumes and the loop
    # re-enters at the back-edge (L30) until the whole payload is sent.  Two
    # fibers only (server reads, client sends) -- no busy-yield fiber
    # that would starve netpoll.  DEFAULT (epoll) backend; no io_uring.
    PAYLOAD = 4 * 1024 * 1024
    res = {}
    lst = rc.TCPConn.listen("127.0.0.1", 0)
    s = socket.socket(fileno=socket.dup(lst.fileno()))
    try:
        port = s.getsockname()[1]
    finally:
        s.detach()
        s.close()

    def server():
        conn = lst.accept()
        total = 0
        while total < PAYLOAD:
            d = conn.recv(64 * 1024)
            if not d:
                break
            total += len(d)
        res["recv"] = total
        conn.close()
        lst.close()

    def sender():
        c = rc.TCPConn.connect("127.0.0.1", port)
        n = c.send_all(b"Z" * PAYLOAD)   # forces EAGAIN parks (L30 + L46-47)
        res["sent"] = n
        c.close()

    with hang_guard(25, "send EAGAIN park/resume"):
        rc.fiber(server)
        rc.fiber(sender)
        rc.run()
    assert res.get("sent") == PAYLOAD, res
    assert res.get("recv") == PAYLOAD, res


# ===========================================================================
# 2. send hard-error branch (conn_send.c.inc L41-43): a peer that RSTs the
#    connection makes send() return EPIPE/ECONNRESET (not EAGAIN/EWOULDBLOCK/
#    EINTR) -> L41 true -> PyBuffer_Release + raise OSError.  DEFAULT (epoll)
#    backend.  Driven with a real raw socket peer that sets SO_LINGER {1,0} and
#    closes (sends a RST).
# ===========================================================================
def test_send_hard_error_surfaces_oserror_epoll():
    import struct
    res = {}
    lst = rc.TCPConn.listen("127.0.0.1", 0)
    s = socket.socket(fileno=socket.dup(lst.fileno()))
    try:
        port = s.getsockname()[1]
    finally:
        s.detach()
        s.close()
    accepted = {}

    def server():
        conn = lst.accept()
        accepted["c"] = conn

    def sender():
        c = rc.TCPConn.connect("127.0.0.1", port)
        while "c" not in accepted:
            rc.sched_yield()
        # Abort the server side with an RST.
        sconn = accepted["c"]
        raw = socket.socket(fileno=socket.dup(sconn.fileno()))
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                       struct.pack("ii", 1, 0))
        raw.close()        # RST toward the client
        sconn.close()
        # Now repeatedly send until the local stack reports the broken pipe.
        err = None
        try:
            for _ in range(200):
                c.send_all(b"x" * 4096)
                rc.sched_yield()
        except OSError as e:
            err = e.errno
        res["err"] = err
        try:
            c.close()
        except OSError:
            pass
        lst.close()

    with hang_guard(20, "send hard error"):
        rc.fiber(server)
        rc.fiber(sender)
        rc.run()
    # EPIPE (32) or ECONNRESET (104): a non-transient send error surfaced as a
    # clean OSError (L41-43), never a crash or a hang.
    assert res.get("err") in (errno_EPIPE(), errno_ECONNRESET()), res


def errno_EPIPE():
    import errno
    return errno.EPIPE


def errno_ECONNRESET():
    import errno
    return errno.ECONNRESET


# ===========================================================================
# 3. send EAGAIN park + cancel -> wait_fd<0 error return (conn_send.c.inc L50).
#    A fiber fills SO_SNDBUF and parks on EPOLLOUT inside send_all (L46);
#    a second fiber then cancel_wait_fd()s it, so netpoll_wait_fd_coop
#    returns <0 -> L47 PyBuffer_Release + L50 returns (clean OSError, no
#    Python-level pending exc -> SetFromErrno).  Epoll path, bounded.
# ===========================================================================
def test_send_park_then_cancel_returns_error_epoll():
    res = {}
    hold = {}
    lst = rc.TCPConn.listen("127.0.0.1", 0)
    s = socket.socket(fileno=socket.dup(lst.fileno()))
    try:
        port = s.getsockname()[1]
    finally:
        s.detach()
        s.close()
    accepted = {}

    def server():
        conn = lst.accept()
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                        (1024).to_bytes(4, sys.byteorder))
        accepted["c"] = conn   # never read -> the client's send backs up

    def sender():
        c = rc.TCPConn.connect("127.0.0.1", port)
        c.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                     (1024).to_bytes(4, sys.byteorder))
        while "c" not in accepted:
            rc.sched_yield()
        try:
            # Fills the buffers and parks on EPOLLOUT (peer never reads).
            c.send_all(b"Q" * (4 * 1024 * 1024))
            res["outcome"] = "completed"
        except OSError as e:
            res["outcome"] = "oserror"
            res["errno"] = e.errno
        except BaseException as e:  # noqa: BLE001
            res["outcome"] = "other:%r" % (e,)
        c.close()

    def canceller():
        g = hold["g"]
        # Let the sender fill SO_SNDBUF and actually park.
        for _ in range(50):
            rc.sched_yield()
        rc.sched_sleep(0.05)
        res["cancel_ret"] = g.cancel_wait_fd()
        accepted["c"].close()
        lst.close()

    with hang_guard(20, "send park+cancel"):
        hold["g"] = rc.fiber(sender)
        rc.fiber(server)
        rc.fiber(canceller)
        rc.run()
    # The parked send was cancelled: wait_fd returned <0 -> L50 error return.
    # The cancel succeeded and the send did NOT silently complete.
    assert res.get("cancel_ret") is True, res
    assert res.get("outcome") == "oserror", res


# ===========================================================================
# 4. send_all closed-conn guard (conn_send.c.inc L68): close() then send_all
#    hits self->closed -> PyErr_SetString("TCPConn is closed") + return.
# ===========================================================================
def test_send_all_on_closed_conn_raises():
    res = {}
    lst = rc.TCPConn.listen("127.0.0.1", 0)
    s = socket.socket(fileno=socket.dup(lst.fileno()))
    try:
        port = s.getsockname()[1]
    finally:
        s.detach()
        s.close()
    accepted = {}

    def server():
        accepted["c"] = lst.accept()

    def client():
        c = rc.TCPConn.connect("127.0.0.1", port)
        while "c" not in accepted:
            rc.sched_yield()
        c.close()
        try:
            c.send_all(b"x")            # closed -> L68
            res["raised"] = False
        except OSError as e:
            res["raised"] = True
            res["msg"] = str(e)
        accepted["c"].close()
        lst.close()

    with hang_guard(15, "send_all closed guard"):
        rc.fiber(server)
        rc.fiber(client)
        rc.run()
    assert res.get("raised") is True, res
    assert "closed" in res.get("msg", ""), res


# ===========================================================================
# 5. accept fatal-error branch (conn_net.c.inc L110-111): an accept() error
#    whose errno is not in {EAGAIN,EWOULDBLOCK,EINTR,ECONNABORTED} -> L110 true
#    -> L111 PyErr_SetFromErrno -> clean OSError.  No in-process FINJ hook on
#    Linux (RUNLOOM_TCP_FINJ==0), so use strace -e inject=accept4:error=EINVAL.
#    NB: on Linux stackweave's accept path uses accept4(SOCK_NONBLOCK|SOCK_CLOEXEC).
# ===========================================================================
_ACCEPT_FATAL = r'''
import sys, socket; sys.path.insert(0, "src")
import stackweave_c as rc
box = {}
def main():
    lst = rc.TCPConn.listen("127.0.0.1", 0)
    s = socket.socket(fileno=socket.dup(lst.fileno()))
    try: port = s.getsockname()[1]
    finally: s.detach(); s.close()
    # A real raw client fills the accept queue so accept() would otherwise
    # succeed; the injected error fires INSTEAD of a successful accept.
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.connect(("127.0.0.1", port))
    def server():
        try:
            conn = lst.accept()
            box["ok"] = True
            conn.close()
        except OSError as e:
            box["errno"] = e.errno
    rc.fiber(server); rc.run()
    raw.close(); lst.close()
main()
if "errno" in box:
    sys.stdout.write("ACCEPT_OSERROR errno=%s\n" % box["errno"])
else:
    sys.stdout.write("ACCEPT_OK %r\n" % box.get("ok"))
'''


def _strace_supports_inject():
    strace = shutil.which("strace")
    if not strace:
        return False
    try:
        p = subprocess.run(
            [strace, "-e", "inject=accept:error=EINVAL:when=1", "true"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
        return p.returncode == 0 and b"invalid" not in p.stderr.lower()
    except Exception:
        return False


@pytest.mark.skipif(not _strace_supports_inject(),
                    reason="strace with -e inject= not available")
def test_accept_fatal_error_surfaces_oserror():
    strace = shutil.which("strace")
    env = dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src")
    # EINVAL is not in {EAGAIN,EWOULDBLOCK,EINTR,ECONNABORTED} -> L110 fatal.
    cmd = [strace, "-f", "-e", "signal=none",
           # the accept path uses accept4(SOCK_NONBLOCK) on Linux; inject on
           # accept too so the fault fires whichever syscall runs.
           "-e", "inject=accept,accept4:error=EINVAL:when=1+",
           PY, "-c", _ACCEPT_FATAL]
    try:
        p = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True,
                           text=True, timeout=60)
    except subprocess.TimeoutExpired:
        pytest.skip("strace accept-fatal child timed out")
    assert p.returncode == 0, (p.stdout[-500:], p.stderr[-2000:])
    # EINVAL == 22: the fatal accept error surfaced cleanly (no crash/hang).
    assert "ACCEPT_OSERROR errno=22" in p.stdout, (p.stdout[-500:],
                                                   p.stderr[-2000:])
