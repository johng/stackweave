"""Shared helpers for the adversarial QA suite (tests/test_adv_*.py).

The adversarial suite deliberately drives the runtime toward its failure
modes: lost wakes, teardown hangs, refcount UAF, fd-reuse staleness,
foreign-OS-thread re-entry, guard-page overflow, and *slow returns* on
non-blocking I/O.  Two infrastructure problems follow from that goal and
this module solves both:

  1. A real hang (a lost wake inside C `run()`/`mn_run()` with no timeout
     argument) cannot be interrupted from Python.  `hang_guard()` arms
     `faulthandler.dump_traceback_later(..., exit=True)`, so a wedged test
     prints every thread's C+Python stack and `_exit`s instead of blocking
     forever.  Under tests/run_isolated.py that surfaces as a per-file
     TIMEOUT-ish crash with a pinpointed traceback, not a dead suite.

  2. "Slow return" is part of the assessment: a cooperative op that *does*
     return but only after starving its siblings is a bug.  `Stopwatch` /
     `assert_faster_than` make an upper-bound wall-clock assertion a
     first-class check, not a flaky afterthought.

`raw_thread()` spawns a **real** OS thread captured from the unpatched
`threading` module, so foreign-OS-thread tests keep a genuine non-fiber
thread even after `stackweave.monkey.patch()` has replaced `threading`.
"""
import faulthandler
import os
import re
import sys
import time
import threading
import contextlib

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

# Captured BEFORE any monkey.patch() in any test could run -- a genuine OS
# thread class + primitives a "foreign thread" test needs to stay foreign.
_RealThread = threading.Thread
# Bound at import, BEFORE any monkeypatching: OverlapTracker records spans from
# blockpool worker threads, so its lock must be a real OS lock rather than a
# cooperative one that would park a thread the scheduler does not manage.
_RealLock = threading.Lock
# Same reason: a rendezvous between genuine executor threads must not be a
# cooperative barrier, which would never release them.
RealBarrier = threading.Barrier
_real_sleep = time.sleep
# The wedge-capture watchdog reuses _RealThread (above) -- it MUST run on a real
# OS thread, since a cooperative thread can't run while the scheduler is wedged,
# which is exactly when we need the dump.  Event is captured pre-patch too.
_real_event = threading.Event


def dump_cooperative_state(label=""):
    """Dump the stackweave COOPERATIVE state for a wedge / lost-wake post-mortem.

    faulthandler shows only the OS-thread (hub) stacks; it cannot show the
    PARKED FIBERS -- which is exactly what a lost-wake wedge looks like.  This
    dumps:
      * dump_fibers   -- every live fiber + its state + the fd it waits on
                         (e.g. ``g1025 io-wait fd=5 ev=R``);
      * _dump_parkers -- the netpoll parkers.  A nonzero ``readyParked`` means an
                         fd was READY but its parker was NOT woken == a LOST WAKE
                         (the smoking gun: data present, fiber still parked);
      * print_hubs    -- per-hub running_g / dwell / pending.
    Safe to call from a foreign OS thread while the scheduler is wedged.
    """
    import stackweave_c as _rc
    tag = " (" + label + ")" if label else ""
    sys.stderr.write("\n[wedge-capture] cooperative-state dump{0}:\n".format(tag))
    sys.stderr.flush()
    for fn in ("dump_fibers", "_dump_parkers"):
        try:
            getattr(_rc, fn)()
        except Exception as e:               # noqa: BLE001
            sys.stderr.write("[wedge-capture] {0} failed: {1!r}\n".format(fn, e))
    # The aio task layer, which dump_fibers cannot see: a WAITING task has no
    # live fiber (its driver returned into a future), so a task wedged above the
    # scheduler appears in the fiber dump only as an absence.  Only meaningful
    # if the aio bridge is actually loaded -- importing it here otherwise would
    # be a surprising side effect mid-wedge.
    _aio = sys.modules.get("stackweave.aio.tasks")
    if _aio is not None:
        try:
            _aio.print_tasks(file=sys.stderr)
        except Exception as e:                   # noqa: BLE001
            sys.stderr.write("[wedge-capture] task dump failed: {0!r}\n".format(e))
    try:
        from stackweave import inspect as _gi
        _gi.print_hubs(file=sys.stderr)
    except Exception as e:                    # noqa: BLE001
        sys.stderr.write("[wedge-capture] print_hubs failed: {0!r}\n".format(e))
    sys.stderr.flush()


@contextlib.contextmanager
def wedge_capture(seconds, label=""):
    """Real-OS-thread watchdog: if the body does not finish within `seconds`,
    dump_cooperative_state(label).  Unlike hang_guard it does NOT abort -- the
    body keeps running (the outer test/run_isolated timeout is the backstop), so
    a recoverable slow path is merely annotated, while a true wedge is captured
    with WHICH fiber parked on WHICH fd instead of an opaque timeout.
    """
    _done = _real_event()

    def _watch():
        if not _done.wait(seconds):
            dump_cooperative_state(label)

    _RealThread(target=_watch, name="wedge_capture", daemon=True).start()
    try:
        yield
    finally:
        _done.set()


@contextlib.contextmanager
def hang_guard(seconds, label="", capture=True):
    """Dump all stacks and _exit if the body does not finish in `seconds`.

    The only reliable watchdog for a hang that lives inside the C scheduler
    with the GIL off: faulthandler runs its timer on a dedicated thread that
    does not need the interpreter to be responsive.  With ``capture`` (default
    on), also dump the stackweave COOPERATIVE state (dump_cooperative_state) just
    before the faulthandler exit, so a lost-wake wedge shows which fiber parked
    on which fd -- not just the opaque OS-thread dump.

    Also forces UNRAISABLE exceptions to be reported the instant they happen.
    A fiber whose body raises does not propagate anywhere -- stackweave captures it
    into g->error and reports it through sys.unraisablehook (see
    STACKWEAVE_GOROUTINE_PANIC).  That report is the single most useful artifact
    when a hang is caused by a fiber dying, because it names the line.  But
    pytest's `unraisableexception` plugin replaces the hook to COLLECT
    unraisables and re-raise them at test TEARDOWN -- and this guard exits via
    faulthandler's exit=True, i.e. _exit(), so teardown never runs and the
    report is discarded exactly when it mattered.  Measured: the same dying
    driver prints a full traceback under `-p no:unraisableexception` and
    nothing at all under stock pytest.  So write it to fd 2 immediately, then
    still delegate to whatever hook was installed.
    """
    if label:
        sys.stderr.write("[hang_guard] arming {0}s for {1}\n".format(seconds, label))
        sys.stderr.flush()
    _done = _real_event()
    if capture:
        def _cap():
            if not _done.wait(max(2.0, seconds * 0.8)):
                dump_cooperative_state(label)
        _RealThread(target=_cap, name="hang_guard_capture", daemon=True).start()

    _prev_hook = sys.unraisablehook

    def _immediate_unraisable(unraisable, _prev=_prev_hook):
        # os.write to fd 2 directly: no buffering to lose if we are _exit()ed
        # moments later, and safe from a foreign thread mid-wedge.
        try:
            import traceback as _tb
            txt = "".join(_tb.format_exception(
                unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback))
            obj = getattr(unraisable, "object", None)
            os.write(2, ("\n[hang_guard] UNRAISABLE in %r%s:\n%s"
                         % (obj,
                            (" -- " + unraisable.err_msg) if getattr(
                                unraisable, "err_msg", None) else "",
                            txt)).encode("utf-8", "replace"))
        except Exception:
            pass
        try:
            _prev(unraisable)          # keep pytest's collection working too
        except Exception:
            pass

    sys.unraisablehook = _immediate_unraisable
    faulthandler.dump_traceback_later(seconds, exit=True)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
        _done.set()
        if sys.unraisablehook is _immediate_unraisable:
            sys.unraisablehook = _prev_hook


class Stopwatch(object):
    def __enter__(self):
        self.t0 = time.monotonic()
        return self

    def __exit__(self, *a):
        self.elapsed = time.monotonic() - self.t0
        return False


@contextlib.contextmanager
def assert_faster_than(seconds, what="operation"):
    """Fail if the body takes longer than `seconds` of wall-clock.

    A 'slow return' guard: the op completes, but cooperative overlap broke
    and it took far longer than the work warranted.
    """
    sw = Stopwatch().__enter__()
    try:
        yield
    finally:
        sw.__exit__()
    assert sw.elapsed < seconds, (
        "{0} took {1:.3f}s, expected < {2:.3f}s (slow return / lost overlap)"
        .format(what, sw.elapsed, seconds))


class OverlapTracker(object):
    """Records when each unit of work actually ran, so a test can assert that
    they OVERLAPPED rather than that they finished by some deadline.

    Wall-clock overlap bounds ("N jobs of 0.15s must finish in under 0.9s")
    read like parallelism assertions but are really machine-speed assertions:
    they pass on a fast box and fail on a loaded CI runner, and they fail by
    milliseconds, which is the signature of a bad instrument rather than a bug.
    That bound flaked seven times on macOS before it was replaced -- once by
    8ms.  Peak concurrency is what the tests actually mean, and it is invariant
    to how fast the machine is: if the work serialises, the peak is 1 no matter
    how quick each unit was; if it overlaps, the peak is >1 no matter how slow.

    Usage:
        ov = OverlapTracker()
        def work():
            with ov.span():
                time.sleep(NAP)
        ...
        ov.assert_peak_at_least(2, "concurrent offload (mn)")
    """

    def __init__(self):
        self.spans = []                      # (t_enter, t_exit) per unit
        self._lock = _RealLock()

    @contextlib.contextmanager
    def span(self):
        t_in = time.monotonic()
        try:
            yield
        finally:
            t_out = time.monotonic()
            with self._lock:
                self.spans.append((t_in, t_out))

    def peak(self):
        """Max number of spans open at once (a sweep over the endpoints)."""
        events = []
        for t_in, t_out in self.spans:
            events.append((t_in, 1))
            events.append((t_out, -1))
        # close before open at equal timestamps: never credit overlap to two
        # spans that merely touched at an endpoint.
        events.sort(key=lambda e: (e[0], e[1]))
        cur = best = 0
        for _, delta in events:
            cur += delta
            if cur > best:
                best = cur
        return best

    def assert_peak_at_least(self, want, what="work"):
        got = self.peak()
        assert got >= want, (
            "{0}: peak concurrency {1}, expected >= {2} over {3} span(s) "
            "-- the work serialised (spans={4!r})"
            .format(what, got, want, len(self.spans), self.spans[:8]))
        return got


def raw_thread(target, *args, **kwargs):
    """A genuine OS thread from the pre-patch threading module."""
    t = _RealThread(target=target, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return t


def free_tcp_port_pair():
    """Return (listen_sock, port) for a bound-but-not-accepted loopback listener."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(128)
    return s, s.getsockname()[1]


def needs_free_threading():
    """True iff this interpreter has the GIL disabled (real M:N parallelism)."""
    return hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled()


def ensure_fd_budget(n, what="this test"):
    """Raise the soft RLIMIT_NOFILE to cover `n` descriptors, or skip.

    macOS ships a soft limit of 256 with an effectively unlimited hard limit
    (capped by kern.maxfilesperproc, 10240 on the CI box), so a test wanting a
    few hundred socketpairs dies with EMFILE on a stock mac while passing on
    Linux, where the soft default is 1024+.  That is an environment
    assumption, not a runtime bug, and it is invisible in the failure -- the
    traceback points at socket.socketpair(), several frames from anything the
    test is actually about.

    Raising the SOFT limit toward the hard one needs no privileges; it is
    exactly what `ulimit -n` does.  Restoring is deliberately NOT attempted:
    lowering it again could break an unrelated later test in the same
    interpreter, and a higher soft limit harms nothing.
    """
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft >= n:
        return
    want = n if hard == resource.RLIM_INFINITY else min(n, hard)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (ValueError, OSError):
        want = soft
    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] < n:
        import pytest
        pytest.skip("%s needs %d fds; soft RLIMIT_NOFILE is %d and could not "
                    "be raised (hard limit %s)" % (what, n, want, hard))


def pollable_pipe():
    """Return (rfd, wfd, keepalive) -- a pair of fds usable as a wait_fd target.

    The netpoll backend (epoll/kqueue/select) can poll a pipe, so this is just
    os.pipe() and `keepalive` is always None (kept for callers that hold it).
    """
    r, w = os.pipe()
    return r, w, None


# ---------------------------------------------------------------------------
# The io_uring loop backend (STACKWEAVE_IOURING_LOOP): each hub blocks in its
# own ring instead of the epoll pump.  A test of the backend appends
# IOURING_LOOP_TRAILER to its child script, runs its own checks, then
# assert_iouring_loop_ran(p): the trailer prints stats()["iouring_loop_waits"]
# (hub ring waits, folded into a process total as each hub tears its ring down),
# which stays 0 if the run fell back to the epoll pump.  The workloads' own
# oracles pass on either backend, so this is what proves the loop ran.
IOURING_LOOP_TRAILER = (
    "\nimport stackweave_c as _rc_iou_trailer\n"
    "print('IOURING_LOOP_WAITS %d' % _rc_iou_trailer.stats()['iouring_loop_waits'])\n")


def assert_iouring_loop_ran(p):
    """Fail unless the child process `p` (its script ending in
    IOURING_LOOP_TRAILER) blocked in a hub io_uring ring at least once."""
    m = re.search(r"IOURING_LOOP_WAITS (\d+)", p.stdout)
    assert m is not None and int(m.group(1)) > 0, (
        "the io_uring loop backend did not run (no hub ring wait)\n"
        + p.stdout[-400:] + "\n" + p.stderr[-800:])


def kernel_pbuf_ring_errno():
    """The errno with which THIS kernel refuses a valid io_uring provided buffer
    ring registration (IORING_REGISTER_PBUF_RING), 0 if it accepts one, or None
    if io_uring itself can't be set up here.

    Multishot recv (STACKWEAVE_IOURING_MS) needs a buffer ring on every hub
    ring and falls back to single-shot recv when the kernel refuses one, so a
    multishot test must tell "the kernel can't" from "the runtime broke".  This
    asks the kernel directly, with raw syscalls and the registration built as
    the UAPI documents it (page-aligned ring, power-of-two entries, zero flags
    and reserved words), so a nonzero result is the kernel's verdict and not a
    runtime bug.  Ubuntu's 6.8.0-142-generic kernel returns EINVAL here for
    every valid call: its reserved-word check is inverted."""
    import ctypes
    import mmap
    import platform
    import struct
    if not sys.platform.startswith("linux") or \
            platform.machine() not in ("x86_64", "aarch64"):
        return None
    nr_setup, nr_register, register_pbuf_ring = 425, 427, 22   # same on both arches
    libc = ctypes.CDLL(None, use_errno=True)
    params = ctypes.create_string_buffer(120)          # struct io_uring_params
    fd = libc.syscall(ctypes.c_long(nr_setup), ctypes.c_long(8), params)
    if fd < 0:
        return None
    ring = mmap.mmap(-1, mmap.PAGESIZE)                # 8 entries x 16 B fit
    cring = ctypes.c_char.from_buffer(ring)
    try:
        reg = ctypes.create_string_buffer(struct.pack(
            "=QIHH3Q", ctypes.addressof(cring), 8, 0, 0, 0, 0, 0))
        rc = libc.syscall(ctypes.c_long(nr_register), ctypes.c_long(fd),
                          ctypes.c_long(register_pbuf_ring), reg,
                          ctypes.c_long(1))
        return ctypes.get_errno() if rc < 0 else 0
    finally:
        os.close(fd)                                   # drops the kernel's pin
        del cring
        ring.close()


_NPROC_PROBE = r"""
import resource, threading
soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
resource.setrlimit(resource.RLIMIT_NPROC, (1, hard))
try:
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    print("NOT_CAPPED")
except RuntimeError:
    print("CAPPED")
"""


def rlimit_nproc_caps_threads():
    """True iff lowering RLIMIT_NPROC actually stops this process creating a
    thread, which the thread-spawn-failure tests rely on.

    It does not for root, nor for any process with CAP_SYS_RESOURCE or
    CAP_SYS_ADMIN: the kernel exempts them from the limit at clone(), so under
    those the tests' "no new threads" limit is ignored and every spawn
    succeeds.  Probed for real in a child (limit 1, start one thread) rather
    than inferred from the euid, since a capability set can exempt a non-root
    user, and root in a container that maps it to an unprivileged host uid is
    capped.  False off Linux, where the limit caps fork()ed processes, not
    threads.  Only an explicit NOT_CAPPED answer returns False: a probe that
    crashes or hangs leaves the tests to run and fail loudly rather than skip
    them everywhere."""
    import subprocess
    if not sys.platform.startswith("linux"):
        return False
    try:
        p = subprocess.run([sys.executable, "-c", _NPROC_PROBE],
                           capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return True
    return p.stdout.strip() != "NOT_CAPPED"


def needs_rlimit_nproc_thread_cap():
    """A skipif mark for tests that force a thread-spawn failure by lowering
    RLIMIT_NPROC: skip, saying why, where the limit can't take effect."""
    import pytest
    capped = rlimit_nproc_caps_threads()
    return pytest.mark.skipif(
        not capped,
        reason="RLIMIT_NPROC does not stop thread creation here (euid %d: the "
               "kernel exempts root and CAP_SYS_RESOURCE/CAP_SYS_ADMIN, and "
               "off Linux it caps processes, not threads), so a thread-spawn "
               "failure can't be forced" % os.geteuid())
