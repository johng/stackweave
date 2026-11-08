"""Edge cases of monkey/osio.py's cooperative os.read/os.write family.

The main contract -- a blocking fd keeps its mode, and the call parks the fiber
rather than the hub -- is guarded by
tests/test_cpython_thread_keyed_locks.py::test_identity_patched_os_read_leaves_the_fd_mode_alone.
Each scenario here runs in a fresh interpreter (monkey.patch is process-wide).
"""
from adv_util import run_python


def _run(code, timeout=60):
    p = run_python(code, timeout=timeout)
    assert p.returncode == 0 and "OK" in p.stdout, (p.stdout, p.stderr[-3000:])


def test_an_fd_the_netpoll_refuses_falls_back_to_the_pool_instead_of_spinning():
    """wait_fd returns < 0 when the netpoll refuses an fd (kqueue rejects some
    character devices).  The EAGAIN loops for a non-blocking fd and a blocking
    socket used to ignore that and retry at once, spinning hot; they now hand
    the call to a pool worker, which waits for the fd itself."""
    _run(r'''
import os, socket, threading, time
import stackweave, stackweave.monkey as monkey
monkey.patch()
from stackweave.monkey import osio
waits = []
def refused(fd, ev, timeout_ms=-1):
    waits.append(fd)
    return -1
osio._wait_fd_coop = refused
def later(fn, *a):
    threading.Thread(target=lambda: (time.sleep(0.2), fn(*a)), daemon=True).start()
out = {}
def main():
    # non-blocking pipe, empty: read waits for a plain thread's write
    r, w = os.pipe(); os.set_blocking(r, False)
    later(os.write, w, b"x")
    out["read"] = os.read(r, 10)
    out["read_waits"] = len(waits)
    # non-blocking pipe, full: write waits for a plain thread to drain it
    os.set_blocking(w, False)
    while True:
        try:
            osio._orig_os_write(w, b"f" * 4096)   # raw: the patched one parks
        except BlockingIOError:
            break
    def drain():
        time.sleep(0.2)                       # after the write found it full
        os.set_blocking(r, True)
        while os.read(r, 1 << 16):
            pass
    threading.Thread(target=drain, daemon=True).start()
    del waits[:]
    out["write"] = os.write(w, b"y")
    out["write_waits"] = len(waits)
    # blocking socket (MSG_DONTWAIT recv), empty: same fallback
    a, b = socket.socketpair()
    fa, fb = a.detach(), b.detach()
    del waits[:]
    later(os.write, fa, b"s")
    out["sock"] = os.read(fb, 10)
    out["sock_waits"] = len(waits)
    out["sock_blocking"] = os.get_blocking(fb)
stackweave.run(2, main)
print(out, flush=True)
assert out["read"] == b"x" and out["read_waits"] == 1, out
assert out["write"] == 1 and out["write_waits"] == 1, out
assert out["sock"] == b"s" and out["sock_waits"] == 1, out
assert out["sock_blocking"] is True, out
print("OK")
''')


def test_an_empty_write_returns_at_once_on_a_full_blocking_pipe():
    """os.write(fd, b'') and os.writev(fd, []) return 0 at once from the
    blocking call; the cooperative ones used to wait for the full pipe to turn
    writable first, which here is never."""
    _run(r'''
import os, time
import stackweave, stackweave.monkey as monkey
monkey.patch()
from stackweave.monkey import osio
out = {}
def main():
    r, w = os.pipe()
    os.set_blocking(w, False)
    while True:                       # fill it, with the raw (unpatched) write
        try:
            osio._orig_os_write(w, b"f" * 4096)
        except BlockingIOError:
            break
    os.set_blocking(w, True)
    def outcome(f, *a):
        try:
            return f(*a)
        except OSError as e:
            return "errno %d" % e.errno
    # writev(fd, []) is 0 on Linux and EINVAL on macOS: whatever the raw call
    # does on an empty pipe, the patched one does on the full one, at once.
    _r2, w2 = os.pipe()
    out["stock_writev"] = outcome(osio._orig_os_writev, w2, [])
    t0 = time.monotonic()
    out["write"] = outcome(os.write, w, b"")
    out["writev"] = outcome(os.writev, w, [])
    out["writev_empties"] = outcome(os.writev, w, [b"", bytearray()])
    out["secs"] = time.monotonic() - t0
    out["blocking"] = os.get_blocking(w)
stackweave.run(2, main)
print(out, flush=True)
assert out["write"] == 0 and out["writev_empties"] == 0, out
assert out["writev"] == out["stock_writev"], out
assert out["secs"] < 2 and out["blocking"] is True, out
print("OK")
''', timeout=30)
