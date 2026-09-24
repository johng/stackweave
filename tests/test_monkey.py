"""Tests for stackweave.monkey -- cooperative patches across the stdlib.

These tests exercise the C scheduler (stackweave_c.fiber / stackweave_c.run)
because that's the path the monkey-patches target.
"""
import os
import queue
import socket
import sys
import threading
import time
import unittest


sys.path.insert(0, "src")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stackweave
import stackweave.monkey
import stackweave_c
from adv_util import OverlapTracker  # noqa: E402


def _drive(fn):
    """Spawn fn as a fiber, run scheduler, return its return value."""
    box = [None, None]
    def runner():
        try:
            box[0] = fn()
        except BaseException as e:
            box[1] = e
    stackweave_c.fiber(runner)
    stackweave_c.run()
    if box[1] is not None:
        raise box[1]
    return box[0]


def tearDownModule():
    """Reverse every monkey-patch this module installs.

    The patches mutate process-global stdlib state (threading.Lock/
    Condition -> cooperative shims, socket.*, os.read/write, builtins.open,
    ...).  Without this, that state leaks into every later test file in the
    same pytest process.  Restoring stdlib here keeps the run
    deterministic."""
    stackweave.monkey.unpatch()


class TestPatchIdempotence(unittest.TestCase):
    def test_double_patch(self):
        stackweave.monkey.patch()
        stackweave.monkey.patch()   # second call is a no-op
        self.assertTrue(callable(time.sleep))
        self.assertTrue(callable(socket.socket.recv))


class TestTimeSleep(unittest.TestCase):
    def test_sleep_interleaves(self):
        """Two patched 0.05 s sleeps must OVERLAP, not serialize.

        This used to be asserted as `elapsed < 0.09` -- two 0.05 s sleeps can
        only finish that fast if they ran concurrently.  True, but not
        measurable on a shared runner: scheduler and timer latency push the
        wall clock to ~0.11-0.13 s even when the sleeps DO overlap (macos-14
        observed 0.119 s, failing 5/5 -- macos-debug run 33943464165).  The
        original TODO here noted the trap and had no way out, because loosening
        the bound to fit would also admit a serialized implementation at
        0.10 s: the bound cannot separate "slow" from "wrong".

        So prove overlap STRUCTURALLY instead, from evidence the test was
        already collecting and then throwing away with sorted().  Interleaved
        execution logs start,start,end,end; a serialized one logs
        start,end,start,end.  That distinction is exact, timing-independent,
        and strictly STRONGER than the old assertion -- sorting the events made
        both orders compare equal, so that half of the test proved nothing at
        all.  The wall clock stays only as a loose backstop against a gross
        regression (a hang, or sleeps an order of magnitude too long), where it
        is doing a job a bound can actually do."""
        stackweave.monkey.patch()
        log = []
        def sleeper(name, dur):
            log.append((name, "start"))
            time.sleep(dur)            # patched -> stackweave.sleep
            log.append((name, "end"))
        stackweave_c.fiber(lambda: sleeper("A", 0.05))
        stackweave_c.fiber(lambda: sleeper("B", 0.05))
        t0 = time.monotonic()
        stackweave_c.run()
        elapsed = time.monotonic() - t0
        events = [e for _, e in log]
        self.assertEqual(events, ["start", "start", "end", "end"],
                         "sleeps serialized rather than overlapped: %r" % (log,))
        self.assertLess(elapsed, 1.0,
                        "two overlapping 0.05s sleeps took %.3fs" % (elapsed,))


class TestThreadingLock(unittest.TestCase):
    def test_lock_excludes_fibers(self):
        stackweave.monkey.patch()
        lock = threading.Lock()
        log = []
        def worker(name):
            with lock:
                log.append((name, "in"))
                stackweave.sleep(0.01)
                log.append((name, "out"))
        stackweave_c.fiber(lambda: worker("A"))
        stackweave_c.fiber(lambda: worker("B"))
        stackweave_c.fiber(lambda: worker("C"))
        stackweave_c.run()
        # Within each pair (in, out) must be adjacent -- no interleaving.
        names = [n for n, _ in log]
        for i in range(0, len(log), 2):
            self.assertEqual(log[i][1], "in")
            self.assertEqual(log[i + 1][1], "out")
            self.assertEqual(log[i][0], log[i + 1][0])


class TestThreadingEvent(unittest.TestCase):
    def test_event_wakes_waiters(self):
        stackweave.monkey.patch()
        ev = threading.Event()
        log = []
        def waiter():
            log.append("wait-start")
            ev.wait()
            log.append("wait-end")
        def setter():
            stackweave.sleep(0.02)
            log.append("set")
            ev.set()
        stackweave_c.fiber(waiter)
        stackweave_c.fiber(waiter)
        stackweave_c.fiber(setter)
        stackweave_c.run()
        self.assertEqual(log.count("wait-start"), 2)
        self.assertEqual(log.count("wait-end"), 2)
        self.assertEqual(log[2], "set")  # both waits started before set
        self.assertEqual(log[-1], "wait-end")


class TestQueue(unittest.TestCase):
    def test_producer_consumer(self):
        stackweave.monkey.patch()
        q = queue.Queue(maxsize=3)
        consumed = []
        def producer():
            for i in range(5):
                q.put(i)
        def consumer():
            for _ in range(5):
                consumed.append(q.get())
        stackweave_c.fiber(producer)
        stackweave_c.fiber(consumer)
        stackweave_c.run()
        self.assertEqual(consumed, [0, 1, 2, 3, 4])


class TestOsReadWrite(unittest.TestCase):
    def test_pipe_round_trip(self):
        stackweave.monkey.patch()
        r, w = os.pipe()
        got = [None]
        def writer():
            time.sleep(0.01)
            os.write(w, b"hello")
            os.close(w)
        def reader():
            got[0] = os.read(r, 1024)
            os.close(r)
        stackweave_c.fiber(reader)
        stackweave_c.fiber(writer)
        stackweave_c.run()
        self.assertEqual(got[0], b"hello")


class TestSelect(unittest.TestCase):
    def test_select_single_fd(self):
        import select
        stackweave.monkey.patch()
        r, w = os.pipe()
        ready_fd = [None]
        def writer():
            time.sleep(0.01)
            os.write(w, b"x")
        def reader():
            rr, _, _ = select.select([r], [], [], 1.0)
            ready_fd[0] = rr
            os.read(r, 1)
        stackweave_c.fiber(reader)
        stackweave_c.fiber(writer)
        stackweave_c.run()
        os.close(r); os.close(w)
        self.assertEqual(ready_fd[0], [r])

    def test_select_timeout(self):
        import select
        stackweave.monkey.patch()
        r, _ = os.pipe()
        result = [None]
        def waiter():
            result[0] = select.select([r], [], [], 0.05)
        stackweave_c.fiber(waiter)
        t0 = time.monotonic()
        stackweave_c.run()
        elapsed = time.monotonic() - t0
        os.close(r)
        self.assertEqual(result[0], ([], [], []))
        self.assertGreaterEqual(elapsed, 0.04)


class TestDNS(unittest.TestCase):
    def test_getaddrinfo_localhost(self):
        stackweave.monkey.patch()
        result = [None]
        def looker():
            result[0] = socket.getaddrinfo("localhost", 80,
                                           type=socket.SOCK_STREAM)
        stackweave_c.fiber(looker)
        stackweave_c.run()
        self.assertIsNotNone(result[0])
        self.assertTrue(len(result[0]) > 0)
        # Should land on 127.0.0.1 or ::1 via /etc/hosts.
        addrs = {info[4][0] for info in result[0]}
        self.assertTrue(addrs & {"127.0.0.1", "::1"})

    def test_getaddrinfo_ip_literal(self):
        stackweave.monkey.patch()
        result = [None]
        def looker():
            result[0] = socket.getaddrinfo("8.8.8.8", 53,
                                           family=socket.AF_INET,
                                           type=socket.SOCK_DGRAM)
        stackweave_c.fiber(looker)
        stackweave_c.run()
        self.assertEqual(result[0][0][4][0], "8.8.8.8")

    def test_getaddrinfo_no_thread_handoff(self):
        # Async DNS must NOT block the scheduler.  Two concurrent lookups
        # should both finish in roughly the time of one.
        stackweave.monkey.patch()
        import stackweave.monkey as M
        # Clear cache so we actually do round-trips.
        M._dns_result_cache.clear()
        times = []
        def looker(name):
            t0 = time.monotonic()
            try:
                socket.getaddrinfo(name, 80, family=socket.AF_INET)
            except Exception:
                pass
            times.append(time.monotonic() - t0)
        stackweave_c.fiber(lambda: looker("localhost"))
        stackweave_c.fiber(lambda: looker("localhost"))
        stackweave_c.run()
        # Both should be sub-second (they hit /etc/hosts, no UDP).
        self.assertTrue(all(t < 0.5 for t in times), times)


class TestFile(unittest.TestCase):
    def test_open_read_regular_file(self):
        import tempfile
        stackweave.monkey.patch()
        path = tempfile.mktemp()
        with open(path, "w") as f:
            f.write("hello stackweave")
        try:
            got = [None]
            def reader():
                with open(path, "r") as f:
                    got[0] = f.read()
            stackweave_c.fiber(reader)
            stackweave_c.run()
            self.assertEqual(got[0], "hello stackweave")
        finally:
            os.unlink(path)

    def test_concurrent_file_reads_interleave(self):
        # Two fibers reading files should overlap via the thread
        # pool -- the scheduler must not be blocked while one reads.
        import tempfile
        stackweave.monkey.patch()
        path = tempfile.mktemp()
        with open(path, "wb") as f:
            f.write(b"x" * 4096)
        try:
            log = []
            def reader(name):
                log.append((name, "start"))
                with open(path, "rb") as f:
                    f.read()
                log.append((name, "done"))
            stackweave_c.fiber(lambda: reader("A"))
            stackweave_c.fiber(lambda: reader("B"))
            stackweave_c.run()
            starts = [e for e in log if e[1] == "start"]
            self.assertEqual(len(starts), 2)
        finally:
            os.unlink(path)


class TestSyscalls(unittest.TestCase):
    def test_stat_listdir(self):
        import tempfile
        stackweave.monkey.patch()
        tmpdir = tempfile.mkdtemp()
        try:
            for nm in ("a.txt", "b.txt"):
                with open(os.path.join(tmpdir, nm), "w") as f:
                    f.write(nm)
            got = [None, None]
            def worker():
                got[0] = sorted(os.listdir(tmpdir))
                got[1] = os.stat(os.path.join(tmpdir, "a.txt")).st_size
            stackweave_c.fiber(worker)
            stackweave_c.run()
            self.assertEqual(got[0], ["a.txt", "b.txt"])
            self.assertEqual(got[1], 5)
        finally:
            import shutil
            shutil.rmtree(tmpdir)


class TestSubprocessWait(unittest.TestCase):
    """Cooperative Popen.wait must not block the scheduler."""

    def test_wait_uses_cooperative_poll(self):
        import subprocess as _sp
        stackweave.monkey.patch()
        SLEEP = 0.2

        def spawn():
            return _sp.Popen([sys.executable, "-c",
                              "import time; time.sleep({})".format(SLEEP)])

        # OVERLAP, measured directly.  This used to time a sequential baseline
        # and require the cooperative run to beat it by SLEEP*0.5.  The ratio
        # was already an improvement on an absolute bound -- it cancels the
        # child's interpreter startup cost -- but it is still a wall clock, and
        # on a loaded runner the cooperative run can lose that half-SLEEP margin
        # while the two waits genuinely did overlap (macOS CI: 0.394s).
        #
        # Peak concurrency answers the actual question -- were both waits in
        # flight at once? -- and is invariant to how slow the box is.  It also
        # drops the baseline entirely, so the test spawns two children instead
        # of four and runs in half the time.
        ov = OverlapTracker()
        log = []
        def waiter(name):
            log.append((name, "start"))
            with ov.span():
                rc = spawn().wait()
            log.append((name, "done", rc))
        stackweave_c.fiber(lambda: waiter("A"))
        stackweave_c.fiber(lambda: waiter("B"))
        stackweave_c.run()

        # If wait() blocked the scheduler, B could not start until A finished
        # and the peak would be 1 however fast the machine is.
        ov.assert_peak_at_least(2, "cooperative Popen.wait")
        self.assertEqual([e[1] for e in log if e[1] == "start"],
                         ["start", "start"])
        for e in log:
            if e[1] == "done":
                self.assertEqual(e[2], 0)


class TestSocketStillWorks(unittest.TestCase):
    """Regression: the original socket patches still work after refactor."""
    def test_echo(self):
        stackweave.monkey.patch()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        port = srv.getsockname()[1]
        result = [None]
        def server():
            conn, _ = srv.accept()
            data = conn.recv(1024)
            conn.sendall(data)
            conn.close()
        def client():
            c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            c.connect(("127.0.0.1", port))
            c.sendall(b"ping")
            result[0] = c.recv(1024)
            c.close()
        stackweave_c.fiber(server)
        stackweave_c.fiber(client)
        stackweave_c.run()
        srv.close()
        self.assertEqual(result[0], b"ping")


if __name__ == "__main__":
    unittest.main()
