"""The monkey shims must tell a fiber from a plain thread correctly.

Every patched lock, sleep and wait asks _in_fiber() which path to take: park
cooperatively on a fiber, or block the OS thread.  It used to read a
thread-local counter that a wrapper on stackweave_c.fiber / mn_fiber bumped
around each fiber's callable, so any fiber left mid-callable kept its thread
marked.  aio.run() leaves one every time a run ends while its keepalive
fiber is asleep: the loop's sched_stop() ends run() there, by design (a later
run lets it exit).  The main thread then answered "in a fiber" for the rest
of the process.  A cooperative RLock then recorded its owner there as
current() -- None -- so a re-entrant acquire failed its own owner check and
waited on itself, in the fiber-only in-memory park, which returns at once
outside a fiber: the re-park loop spun forever.  logging.shutdown() at exit
(acquire, then flush's `with self.lock`) on a StreamHandler was enough:
`requests.get` under aio.run hung on exit, because charset_normalizer adds a
StreamHandler when imported.  _in_fiber() now asks the runtime
(stackweave_c.in_fiber()).

Each scenario runs in a subprocess: the failure is a hang at interpreter
exit, which only a fresh process can show.
"""
import os
import subprocess
import sys

import pytest

import stackweave_c

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(code, timeout=60):
    env = dict(os.environ, PYTHON_GIL="0", STACKWEAVE_GIL="0",
               PYTHONPATH=os.path.join(REPO, "src"))
    try:
        p = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                           timeout=timeout, capture_output=True, text=True)
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode()
        pytest.fail("the process did not exit within %ss (hung at exit?); "
                    "stdout: %s" % (timeout, out[-500:]), pytrace=False)
    return p


_AFTER_AIO_RUN = r'''
import faulthandler, logging, os, socket, sys
faulthandler.dump_traceback_later(20, exit=True)
import stackweave, stackweave_c
stackweave.monkey.patch()
from stackweave.monkey import _base
# A handler whose lock is a cooperative RLock: created after patch().
logging.getLogger("detect").addHandler(logging.StreamHandler(sys.stderr))
async def main():
    # Any call through the blocking pool; the run then ends while the loop's
    # keepalive fiber is asleep, leaving it parked.
    return len(socket.getaddrinfo("localhost", 80)) > 0
assert stackweave.aio.run(main())
print("in_fiber_after_run=%s" % _base._in_fiber(), flush=True)
assert stackweave_c.in_fiber() is False
print("PASS", flush=True)
# logging.shutdown() runs at exit and acquires the handler's lock.
'''


def test_main_thread_is_not_a_fiber_after_aio_run():
    p = _run(_AFTER_AIO_RUN)
    assert "in_fiber_after_run=False" in p.stdout, (
        "the main thread still reports fiber context after aio.run() "
        "returned\nstdout=%s\nstderr=%s" % (p.stdout, p.stderr[-1500:]))
    assert p.returncode == 0 and "PASS" in p.stdout, (
        "rc=%s\nstdout=%s\nstderr=%s" % (p.returncode, p.stdout, p.stderr[-1500:]))


def test_in_fiber_answers_from_any_thread():
    """in_fiber() is True only on a running fiber: false on the main thread
    outside run(), true inside single-thread and M:N fibers, false on a
    plain OS thread started while M:N is live."""
    p = _run(r'''
import threading
import stackweave, stackweave_c
seen = {"main": stackweave_c.in_fiber()}
def single():
    seen["single"] = stackweave_c.in_fiber()
stackweave_c.fiber(single)
stackweave_c.run()
def mn():
    seen["mn"] = stackweave_c.in_fiber()
    t = threading.Thread(target=lambda: seen.__setitem__("thread", stackweave_c.in_fiber()))
    t.start(); t.join()
stackweave.run(2, mn)
seen["after"] = stackweave_c.in_fiber()
print(sorted(seen.items()), flush=True)
assert seen == {"main": False, "single": True, "mn": True,
                "thread": False, "after": False}, seen
print("PASS", flush=True)
''')
    assert p.returncode == 0 and "PASS" in p.stdout, (
        "rc=%s\nstdout=%s\nstderr=%s" % (p.returncode, p.stdout, p.stderr[-1500:]))


def test_in_fiber_is_exported():
    assert stackweave_c.in_fiber() is False
