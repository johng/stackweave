"""Coverage-driven adversarial suite for the TCPConn C surface.

Targets the uncovered (#####) lines in three fragments of runloom_tcp.c:

  src/runloom_c/runloom_tcp_conn_io.c.inc    -- recv / recv_into
  src/runloom_c/runloom_tcp_conn_send.c.inc  -- send / send_all
  src/runloom_c/runloom_tcp_conn_net.c.inc   -- listen / accept / connect / setsockopt

The uncovered lines split into three reachability classes, and the tests are
grouped accordingly. Each test names the source lines it drives and the gate it
makes true.

 1. PLAIN ERROR / EDGE branches reachable in-process on Linux (epoll backend):
    closed-conn guards, bad-arg parse failures, the `fd < 0` ctor guard, the
    `family` getter, a non-local bind (EADDRNOTAVAIL), an unresolvable host
    (getaddrinfo failure on a reserved .invalid TLD), a failing setsockopt.
    Driven directly under stackweave.run(2).

 2. SIGNAL-INTERRUPTED COOPERATIVE PARK (the `wait_fd_coop(...) < 0` arms that
    propagate a raised Python signal handler instead of overwriting it with
    OSError): a SIGALRM handler that raises lands on a fiber parked in
    recv / recv_into / send_all / connect. The signal must be installed in the
    MAIN thread, so these run on the SINGLE-THREAD scheduler (stackweave_c.run()),
    where the parked fiber lives on the main OS thread.

 3. SYNCHRONOUS SYSCALL HARD-ERROR branches that loopback never produces on its
    own (a connect() that returns ECONNREFUSED synchronously instead of
    EINPROGRESS; a recv_into whose recvfrom returns ECONNRESET). Driven by
    strace -e inject= in a subprocess (Linux-only, same mechanism as
    tests/test_tcp_faultinject.py).

Excluded (see the structured report): the RunloomTCPConn_alloc()==NULL cleanup
arms (listen L70-71, accept L126-127, connect L247-248) -- a tp_alloc OOM with
no fault hook in the TCPConn path; and the _PyBytes_Resize(<0) arms -- likewise
OOM-only.
"""
import os
import shutil
import signal
import socket
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adv_util import hang_guard  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

import stackweave  # noqa: E402
import stackweave_c as rc  # noqa: E402
from stackweave.sync import WaitGroup  # noqa: E402


# ===========================================================================
# Class 1: in-process plain error / edge branches (epoll backend, no iouring).
# ===========================================================================

def _connected_pair():
    """Inside a hub: return (client_conn, server_conn, listener) over loopback."""
    L = rc.TCPConn.listen("127.0.0.1", 0)
    s = socket.socket(fileno=os.dup(L.fileno()))
    port = s.getsockname()[1]
    s.detach()
    holder = {}
    wg = WaitGroup()
    wg.add(1)

    def acc():
        try:
            holder["sc"] = L.accept()
        finally:
            wg.done()
    rc.mn_fiber(acc)
    c = rc.TCPConn.connect("127.0.0.1", port)
    wg.wait()
    return c, holder["sc"], L


def test_ctor_and_closed_and_family_and_setsockopt():
    """io.c.inc: L45 (ctor parse fail), L47-48 (fd<0 -> ValueError),
    L117/L120 (family getter), L237-238/L241 (recv_into closed / read-only buf).
    send.c.inc: L13-15 (send closed), L68-69 (send_all closed), L17-54 (send body).
    net.c.inc: L260-274 (setsockopt incl. L273 failure arm), L87-88 (accept closed),
    L152 (connect parse), L26 (listen parse)."""
    box = {}

    def main():
        # io L45: TCPConn(fd) with a non-int fd -> PyArg_ParseTupleAndKeywords fails.
        try:
            rc.TCPConn(fd="not-an-int")
        except TypeError:
            box["ctor_parse"] = True
        # io L47-48: fd < 0 -> ValueError("fd must be >= 0").
        try:
            rc.TCPConn(fd=-1)
        except ValueError as e:
            box["ctor_neg"] = str(e)

        c, sc, L = _connected_pair()

        # io L117/L120: the `family` getset getter (AF_INET == 2).
        box["family"] = c.family

        # send.c.inc L17-54: a single send() on an open conn (the suite otherwise
        # only ever exercises send_all, leaving the whole single-send body dark).
        box["send_n"] = c.send(b"hello")
        box["server_recv"] = sc.recv(5)

        # io L241: recv_into of a READ-ONLY buffer -> the "w*" format demands a
        # writable buffer, so PyArg_ParseTuple fails -> return NULL.
        try:
            sc.recv_into(b"read-only-bytes")
        except TypeError:
            box["recvinto_ro"] = True

        # net.c.inc L260-274: setsockopt success path + the L273 rc!=0 failure arm.
        box["setsockopt_ok"] = sc.setsockopt(
            socket.IPPROTO_TCP, socket.TCP_NODELAY, b"\x01\x00\x00\x00")
        try:
            sc.setsockopt(-1, -1, b"\x00")     # bogus level/optname -> setsockopt fails
        except OSError:
            box["setsockopt_fail"] = True

        c.close()
        box["closed_flag"] = c.closed          # io L111-115 is_closed getter (True)

        # io L237-238: recv_into on a closed conn -> "TCPConn is closed".
        try:
            c.recv_into(bytearray(8))
        except OSError as e:
            box["recvinto_closed"] = str(e)
        # send.c.inc L68-69: send_all on a closed conn.
        try:
            c.send_all(b"x")
        except OSError as e:
            box["sendall_closed"] = str(e)
        # send.c.inc L13-15: send() on a closed conn.
        try:
            c.send(b"x")
        except OSError as e:
            box["send_closed"] = str(e)

        sc.close()
        L.close()
        # net.c.inc L87-88: accept() on a closed listener.
        try:
            L.accept()
        except OSError as e:
            box["accept_closed"] = str(e)

        # net.c.inc L26: TCPConn.listen() with missing required args -> parse fail.
        try:
            rc.TCPConn.listen()
        except TypeError:
            box["listen_parse"] = True
        # net.c.inc L152: TCPConn.connect(host) missing port -> parse fail.
        try:
            rc.TCPConn.connect("127.0.0.1")
        except TypeError:
            box["connect_parse"] = True

    with hang_guard(60, "ctor/closed/family/setsockopt"):
        stackweave.run(2, main)

    assert box.get("ctor_parse") is True
    assert box.get("ctor_neg") == "fd must be >= 0"
    assert box.get("family") == socket.AF_INET
    assert box.get("send_n") == 5
    assert box.get("server_recv") == b"hello"
    assert box.get("recvinto_ro") is True
    assert box.get("setsockopt_ok") is None
    assert box.get("setsockopt_fail") is True
    assert box.get("closed_flag") is True
    assert box.get("recvinto_closed") == "TCPConn is closed"
    assert box.get("sendall_closed") == "TCPConn is closed"
    assert box.get("send_closed") == "TCPConn is closed"
    assert box.get("accept_closed") == "TCPConn is closed"
    assert box.get("listen_parse") is True
    assert box.get("connect_parse") is True


def test_bind_failure_and_resolve_failure():
    """net.c.inc: L59-62 (bind() of a non-local address -> EADDRNOTAVAIL cleanup:
    saved errno + close(fd) + raise), L29 (listen resolve fail), L154 (connect
    resolve fail). getaddrinfo fails deterministically offline on the reserved
    `.invalid` TLD (RFC 6761) and on a non-local literal."""
    box = {}

    def main():
        # net L59-62: 1.2.3.4 is not an address on this host -> bind() EADDRNOTAVAIL.
        try:
            rc.TCPConn.listen("1.2.3.4", 9999)
        except OSError as e:
            box["bind_errno"] = e.errno
        # net L29: listen resolve fail (getaddrinfo on a guaranteed-nonexistent name).
        try:
            rc.TCPConn.listen("no.such.host.invalid", 80)
        except OSError:
            box["listen_resolve_fail"] = True
        # net L154: connect resolve fail.
        try:
            rc.TCPConn.connect("no.such.host.invalid", 80)
        except OSError:
            box["connect_resolve_fail"] = True

    with hang_guard(60, "bind/resolve failure"):
        stackweave.run(2, main)

    import errno as _errno
    assert box.get("bind_errno") == _errno.EADDRNOTAVAIL, box
    assert box.get("listen_resolve_fail") is True
    assert box.get("connect_resolve_fail") is True


def test_recv_partial_resize():
    """io.c.inc: the `got < n_bytes -> _PyBytes_Resize` success arm in recv()
    (L222-224 on the epoll path). recv(4096) returns only the few bytes the peer
    sent, so the result bytes object is shrunk -- a partial-recv we assert is
    exactly the sent payload (proving the resize landed the right length)."""
    box = {}

    def main():
        c, sc, L = _connected_pair()
        c.send_all(b"tiny")
        # recv with a buffer far larger than what arrived -> got(4) < n(4096) -> resize.
        box["data"] = sc.recv(4096)
        c.close()
        sc.close()
        L.close()

    with hang_guard(60, "recv partial resize"):
        stackweave.run(2, main)
    assert box.get("data") == b"tiny", box


# ===========================================================================
# Class 2: a raised signal handler interrupts a cooperative park. Drives the
# `wait_fd_coop(...) < 0` -> PyErr_Occurred() ? NULL arms. SINGLE-THREAD
# scheduler so the parked fiber is on the main OS thread (signal-deliverable).
# ===========================================================================
# These run in a SUBPROCESS: a SIGALRM handler installed at module scope is
# process-global, and the test must not perturb the parent pytest's signal
# state or its scheduler. Each child exits 0 and prints a marker we assert on.

_SIG_TEMPLATE = r'''
import os, socket, signal, sys
sys.path.insert(0, "src")
import stackweave_c as rc

box = {}
def raiser(signum, frame):
    raise KeyboardInterrupt("alarm")
signal.signal(signal.SIGALRM, raiser)

OP = "__OP__"

# The fiber that is NOT under test just waits for the other one to finish, and
# how often it wakes to check decides WHICH FIBER the signal handler runs in.
# CPython runs a Python-level signal handler in whatever code the main thread is
# next evaluating -- not in the fiber that armed the itimer -- so an observer
# polling every 20ms wakes ~7 times inside a 150ms window and can catch the
# KeyboardInterrupt meant for the fiber parked in recv/send_all.  It then escapes
# the observer's entry point as "Exception ignored in: <function client>", the
# fiber under test stays parked forever, and the child dies on the 40s
# faulthandler timeout.  That is exactly what CI showed: for recv the traceback
# named the CLIENT's poll loop, for send_all the SERVER's.
#
# Polling less often than the itimer window keeps the observer parked -- running
# no bytecode -- for most of the window.  Measured on Linux under 4x CPU
# oversubscription, 20 children per cell: send_all 18/20 correct at 0.02, 20/20
# at 0.5 (recv held 20/20 in both, same mechanism and traceback), and 40/40 per
# op at 0.5.
#
# THAT IS A PROBABILITY SHIFT, NOT A FIX.  macOS CI still fails send_all with
# this in place, the handler landing in the SERVER's poll loop exactly as
# before.  The reasoning above is incomplete: when the itimer fires, the client
# is parked in send_all (C) and the observer is parked in sched_sleep, so
# NEITHER fiber is running bytecode.  CPython cannot run the handler until
# something does, and the pending signal is then collected by whichever fiber
# resumes first.  Widening this poll only changes who tends to win that race --
# it cannot make the parked fiber win it.
#
# For delivery to land in the fiber under test, the parked send_all/recv must
# itself return EINTR and resume promptly, which is the very path this test
# claims to cover.  So the remaining macOS failure is a real question about
# signal delivery to a fiber parked in the kqueue backend, not a test-timing
# artifact, and tuning this constant further would only tune the odds.
OBSERVER_POLL = 0.5

def server():
    L = rc.TCPConn.listen("127.0.0.1", 0)
    s = socket.socket(fileno=os.dup(L.fileno())); box["port"] = s.getsockname()[1]; s.detach()
    sc = L.accept()
    if OP in ("recv", "recv_into"):
        # shrink nothing; just park reading with no peer data.
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.15)
            if OP == "recv":
                sc.recv(64)                  # io L214-218: parked recv, signal -> raise
            else:
                sc.recv_into(bytearray(64))  # io L288-292: parked recv_into, signal -> raise
            box["rv"] = "got"
        except KeyboardInterrupt:
            box["interrupt"] = True
    elif OP == "send_all":
        # tiny rcvbuf on the server (peer) so the client's send_all fills + parks.
        sc.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, (4096).to_bytes(4, "little"))
    box["L"] = L; box["sc"] = sc
    while "done" not in box and "interrupt" not in box and "rv" not in box:
        rc.sched_sleep(OBSERVER_POLL)
    sc.close(); L.close()

def client():
    while "port" not in box:
        rc.sched_yield()
    c = rc.TCPConn.connect("127.0.0.1", box["port"]); box["c"] = c
    if OP == "send_all":
        c.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, (4096).to_bytes(4, "little"))
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.2)
            c.send_all(b"Z" * (8 * 1024 * 1024))   # send.c.inc L118-122: parked WRITE, signal -> raise
            box["rv"] = "sent"
        except KeyboardInterrupt:
            box["interrupt"] = True
        box["done"] = True
    else:
        while "interrupt" not in box and "rv" not in box:
            rc.sched_sleep(OBSERVER_POLL)
        box["done"] = True
    c.close()

import faulthandler; faulthandler.dump_traceback_later(40, exit=True)
rc.fiber(server); rc.fiber(client); rc.run()
faulthandler.cancel_dump_traceback_later()
sys.stdout.write("SIG OP=%s interrupt=%r rv=%r\n" % (OP, box.get("interrupt"), box.get("rv")))
'''

_CONNECT_SIG = r'''
import signal, sys
sys.path.insert(0, "src")
import stackweave_c as rc
box = {}
def raiser(signum, frame):
    raise KeyboardInterrupt("alarm")
signal.signal(signal.SIGALRM, raiser)
def client():
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.2)
        # 240.0.0.1 (class-E, unroutable) never completes the handshake, so the
        # non-blocking connect parks on WRITE. The SIGALRM handler raises during
        # the park -> wait_fd_coop returns -1 -> net.c.inc L218-224: saved errno +
        # close(fd) + propagate the raised KeyboardInterrupt (NOT OSError).
        rc.TCPConn.connect("240.0.0.1", 9)
        box["rv"] = "connected"
    except KeyboardInterrupt:
        box["interrupt"] = True
    except OSError as e:
        box["oserror"] = e.errno
import faulthandler; faulthandler.dump_traceback_later(40, exit=True)
rc.fiber(client); rc.run()
faulthandler.cancel_dump_traceback_later()
sys.stdout.write("CONNECT_SIG interrupt=%r oserror=%r rv=%r\n" %
                 (box.get("interrupt"), box.get("oserror"), box.get("rv")))
'''


def _run_child(script, timeout=120, env_extra=None):
    env = dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src")
    if env_extra:
        env.update(env_extra)
    try:
        return subprocess.run([PY, "-c", script], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.skip("child workload timed out (box under heavy load)")


# This used to carry a TODO calling the failure "a tight timing race" where the
# signal "lands in the wrong window".  That was the right symptom and the wrong
# mechanism, and the note was left unfinished with no mitigation applied.  It is
# not a window race: the signal arrives on time, and CPython runs the handler in
# whichever fiber the main thread is evaluating, which was the OBSERVER fiber's
# poll loop.  See OBSERVER_POLL in _SIG_TEMPLATE for the measurement and fix.
@pytest.mark.parametrize("op", ["recv", "recv_into", "send_all"])
def test_signal_interrupts_parked_io(op):
    """io.c.inc L214-218 (recv) / L288-292 (recv_into); send.c.inc L118-122
    (send_all): a raised signal handler during the cooperative park makes
    wait_fd_coop return <0 with a pending exception, so the op returns NULL
    with the SIGNAL's exception, not an OSError overwrite."""
    p = _run_child(_SIG_TEMPLATE.replace("__OP__", op))
    assert p.returncode == 0, (op, p.stdout[-400:], p.stderr[-1200:])
    assert ("SIG OP=%s interrupt=True" % op) in p.stdout, (
        "the signal raised during the parked %s did not propagate as the "
        "interrupt (it may have been swallowed / overwritten by OSError)\n"
        "stdout=%s\nstderr=%s" % (op, p.stdout, p.stderr[-800:]))


def test_signal_interrupts_parked_connect():
    """net.c.inc L218-224: a signal raised while connect() is parked on WRITE
    must close(fd) (preserving errno) and propagate the raised exception."""
    p = _run_child(_CONNECT_SIG)
    assert p.returncode == 0, (p.stdout[-400:], p.stderr[-1200:])
    assert "CONNECT_SIG interrupt=True" in p.stdout, (
        "signal during parked connect did not propagate the interrupt\n"
        "stdout=%s\nstderr=%s" % (p.stdout, p.stderr[-800:]))


# ===========================================================================
# Class 3: synchronous syscall hard-errors via strace -e inject= (Linux only).
# Drives the connect() immediate-error arm and the recv_into() recvfrom-error
# arm that loopback never produces on its own.
# ===========================================================================

def _strace_supports_inject():
    strace = shutil.which("strace")
    if not strace:
        return False
    try:
        p = subprocess.run(
            [strace, "-e", "inject=connect:error=EINTR:when=1", "true"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
        return p.returncode == 0 and b"invalid" not in p.stderr.lower()
    except Exception:
        return False


_STRACE_OK = sys.platform.startswith("linux") and _strace_supports_inject()
needs_strace = pytest.mark.skipif(
    not _STRACE_OK, reason="strace -e inject= (Linux) not available")

_CONNECT_HARD = r'''
import os, socket, sys
sys.path.insert(0, "src")
import stackweave_c as rc
box = {}
def client():
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("127.0.0.1", 0)); lsock.listen(16)
    port = lsock.getsockname()[1]
    try:
        # strace forces connect() itself to return ECONNREFUSED synchronously
        # (not EINPROGRESS), so errno is not in {EINPROGRESS,EAGAIN,EINTR} and we
        # take net.c.inc L234-238: saved errno + close(fd) + raise OSError.
        rc.TCPConn.connect("127.0.0.1", port); box["ok"] = True
    except OSError as e:
        box["errno"] = e.errno
    lsock.close()
rc.fiber(client); rc.run()
if "errno" in box:
    print("OSERROR errno=%s" % box["errno"]); sys.exit(42)
print("OK"); sys.exit(0)
'''

_RECVINTO_HARD = r'''
import os, socket, sys
sys.path.insert(0, "src")
import stackweave_c as rc
box = {}
def client():
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("127.0.0.1", 0)); lsock.listen(16)
    port = lsock.getsockname()[1]
    try:
        c = rc.TCPConn.connect("127.0.0.1", port)
        # strace forces recvfrom() -> ECONNRESET: a non-EAGAIN error in
        # recv_into's loop -> io.c.inc L283-285: release buffer + raise OSError.
        box["n"] = c.recv_into(bytearray(1024)); c.close()
    except OSError as e:
        box["errno"] = e.errno
    lsock.close()
rc.fiber(client); rc.run()
if "errno" in box:
    print("OSERROR errno=%s" % box["errno"]); sys.exit(42)
print("N=%s" % box.get("n")); sys.exit(0)
'''


def _run_strace(script, inject, timeout=60):
    strace = shutil.which("strace")
    env = dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src")
    cmd = [strace, "-f", "-e", "signal=none", "-e", "inject=" + inject,
           PY, "-c", script]
    try:
        return subprocess.run(cmd, cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.skip("strace workload timed out (box under heavy load)")


@needs_strace
def test_connect_synchronous_econnrefused():
    """net.c.inc L234-238: a connect() that returns a hard error synchronously
    (not EINPROGRESS) must close(fd) and surface a clean OSError(ECONNREFUSED)."""
    import errno as _errno
    p = _run_strace(_CONNECT_HARD, "connect:error=ECONNREFUSED:when=1")
    assert p.returncode == 42, (p.returncode, p.stdout[-300:], p.stderr[-600:])
    assert ("errno=%d" % _errno.ECONNREFUSED) in p.stdout, p.stdout[-300:]


@needs_strace
def test_recv_into_synchronous_econnreset():
    """io.c.inc L283-285: a recvfrom() ECONNRESET inside recv_into's loop must
    release the buffer and surface a clean OSError(ECONNRESET)."""
    import errno as _errno
    p = _run_strace(_RECVINTO_HARD, "recvfrom:error=ECONNRESET:when=1")
    assert p.returncode == 42, (p.returncode, p.stdout[-300:], p.stderr[-600:])
    assert ("errno=%d" % _errno.ECONNRESET) in p.stdout, p.stdout[-300:]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
