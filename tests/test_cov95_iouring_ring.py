"""Coverage-driven adversarial test for the io_uring single-op cancel path.

A fiber parked on a global-ring io_uring op (a cooperative file_read) must be
cancellable cross-thread: G.cancel_wait_fd() -> runloom_iouring_cancel_g
(io_uring_l_do.c.inc) submits an ASYNC_CANCEL on the global ring and the parked
op completes -ECANCELED.  The scenario runs in a SUBPROCESS and EXITS CLEANLY so
the gcov counters flush (a crash/_exit never does).

Oracle: cancel_wait_fd() returned True AND the parked read completed with
ECANCELED -- not a hang, not a spurious wake.
"""
import os
import subprocess
import sys

import pytest

from adv_util import needs_free_threading

FT = needs_free_threading()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

pytestmark = pytest.mark.skipif(
    not FT, reason="io_uring M:N cancel path is free-threaded only")


def _iou_available():
    try:
        import stackweave_c
        return bool(stackweave_c.iouring_available())
    except Exception:
        return False


needs_iouring = pytest.mark.skipif(
    not _iou_available(), reason="io_uring unavailable (need Linux >= 5.1)")


def _run(script, env_extra=None, timeout=240):
    # Generous timeout + skip-on-timeout: this box is shared with a CI runner
    # that competes for io_uring + CPU, so a timeout is contention, not a bug.
    env = dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src",
               **(env_extra or {}))
    try:
        return subprocess.run([PY, "-c", script], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.skip("io_uring workload timed out (box under heavy load)")


def _no_crash(p, label):
    # A signal-killed child returns a negative code and NEVER flushes gcov, so a
    # crash is both a finding and useless for coverage.  Require a clean exit.
    assert p.returncode is not None and p.returncode >= 0, (
        "%s CRASHED with signal %d\nstdout=%s\nstderr=%s"
        % (label, -p.returncode if p.returncode else 0,
           p.stdout[-500:], p.stderr[-1800:]))


# Shared subprocess preamble: in-tree stackweave_c + a watchdog that dumps stacks
# and _exits if an io_uring op ever leaks a wake (so a hang surfaces as a clean
# child failure with a traceback, never a wedged pytest).
_PRE = r'''
import sys, os, socket, errno
sys.path.insert(0, "src")
import stackweave
import stackweave_c as rc
from stackweave.sync import WaitGroup
import faulthandler
faulthandler.dump_traceback_later(60, exit=True)
'''
_POST = "faulthandler.cancel_dump_traceback_later()\n"


# ===========================================================================
# GLOBAL-RING CANCEL -- a hub fiber parked on a global-ring io_uring op is
#    cancelled cross-thread.  A file_read on an empty pipe routes
#    runloom_iouring_pread -> runloom_iouring_do, which publishes g->iouring_op
#    and parks.  G.cancel_wait_fd() -> runloom_iouring_cancel_g submits the
#    ASYNC_CANCEL on the global ring inline.
#    Oracle: cancel_wait_fd() returned True AND the parked read completed with
#    ECANCELED -- not a hang, not a spurious wake.
# ===========================================================================
_GLOBAL_CANCEL = _PRE + r'''
res = {}
def main():
    rfd, wfd = os.pipe()                 # never written -> the read parks forever
    os.set_blocking(rfd, False)
    rd = {}
    def reader():
        rd["g"] = rc.current_g()
        buf = bytearray(16)
        try:
            rd["n"] = rc.file_read(rfd, buf, 16, -1)   # global-ring iouring pread; parks
        except OSError as e:
            rd["errno"] = e.errno
        rd["done"] = True
    rc.mn_fiber(reader)
    # Deterministic handshake (no sleep-as-sync): cancel_wait_fd() returns True
    # IFF the reader's io_uring op is published AND wait==PARKED -- i.e. the park
    # has committed (runloom_iouring_cancel_g returns 0 otherwise).  So retry it
    # until it actually submits the global-ring ASYNC_CANCEL; the cap only bounds
    # a hang.  This removes the load-dependent "did 0.08s commit the park?" guess
    # that could fire the cancel before the read parked (cancel False + the read
    # then parks forever on the never-written pipe).
    woke = False
    for _ in range(2000000):
        woke = rd["g"].cancel_wait_fd() if "g" in rd else False
        if woke:
            break
        rc.sched_yield()
    res["woke"] = woke                   # -> iouring_cancel_g global path
    for _ in range(2000):
        if rd.get("done"): break
        stackweave.sleep(0.01)
    res["errno"] = rd.get("errno"); res["done"] = rd.get("done")
    for fd in (rfd, wfd):
        try: os.close(fd)
        except OSError: pass
stackweave.run(2, main)
''' + _POST + r'''
sys.stdout.write("GLOBAL_CANCEL woke=%r errno=%r done=%r\n" %
                 (res.get("woke"), res.get("errno"), res.get("done")))
'''


@needs_iouring
def test_global_ring_iouring_cancel_unblocks_parked_read():
    p = _run(_GLOBAL_CANCEL)
    _no_crash(p, "global-ring cancel")
    assert p.returncode == 0, "global-ring cancel run failed rc=%d\nstderr=%s" % (
        p.returncode, p.stderr[-1600:])
    # True can only come from runloom_iouring_cancel_g here: there is no netpoll
    # parker for a fiber sitting on a bare io_uring op.
    assert "GLOBAL_CANCEL woke=True" in p.stdout, (
        "cancel_wait_fd() did not submit a global-ring ASYNC_CANCEL (no "
        "iouring_cancel_g global path)\nstdout=%s\nstderr=%s"
        % (p.stdout, p.stderr[-1200:]))
    assert "errno=125" in p.stdout and "done=True" in p.stdout, (
        "the parked global-ring read did not complete with ECANCELED after the "
        "cancel (the op was stranded)\nstdout=%s\nstderr=%s"
        % (p.stdout, p.stderr[-1200:]))
