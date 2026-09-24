"""Regression: many fibers doing offloaded I/O concurrently must not
deadlock (single-thread AND multi-hub M:N).

Before the _thread.allocate_lock fix this froze the scheduler: file work goes
through tempfile.gettempdir(), whose `_once_lock` was a REAL _thread lock (only
threading.Lock was patched).  Goroutine A held it across the yielding offloaded
probe while fiber B blocked on the real lock on the same scheduler thread
-> deadlock.  A hung run is caught by run_isolated's per-file timeout.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import stackweave.monkey
import stackweave_c

stackweave.monkey.patch()

import tempfile     # imported AFTER patch, so its _once_lock is cooperative


def _filework():
    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, b"z" * 128)
        os.close(fd)
        with open(path, "rb") as fh:
            fh.read()
    finally:
        os.unlink(path)


def test_concurrent_offload_single_thread():
    for _ in range(16):
        stackweave_c.fiber(_filework, stack_size=2 << 20)
    stackweave_c.run()
    assert stackweave_c._self_check(0) == 0


def test_concurrent_offload_mn():
    stackweave_c.mn_init(4)
    for _ in range(16):
        stackweave_c.mn_fiber(_filework)
    stackweave_c.mn_run()
    stackweave_c.mn_fini()
    assert stackweave_c._self_check(0) == 0
