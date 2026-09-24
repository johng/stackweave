"""Smoke tests: Coro primitive + fiber()/run() scheduler."""
import sys
import time
import unittest

sys.path.insert(0, "src")

import stackweave
import stackweave_c


class TestCoreBackend(unittest.TestCase):
    def test_backend_name(self):
        b = stackweave_c.backend()
        self.assertIn(b, ("ucontext", "fibers", "fcontext-asm"))


class TestCoroPrimitive(unittest.TestCase):
    def test_yield_resume_chain(self):
        log = []
        def child():
            log.append("a")
            stackweave_c.yield_()
            log.append("b")
            stackweave_c.yield_()
            log.append("c")
            return "done"

        c = stackweave_c.Coro(child)
        self.assertFalse(c.done)
        c.resume(); self.assertEqual(log, ["a"]); self.assertFalse(c.done)
        c.resume(); self.assertEqual(log, ["a", "b"]); self.assertFalse(c.done)
        c.resume(); self.assertEqual(log, ["a", "b", "c"]); self.assertTrue(c.done)
        self.assertEqual(c.result, "done")

    def test_exception_propagates_on_resume(self):
        def child():
            raise ValueError("boom")
        c = stackweave_c.Coro(child)
        with self.assertRaises(ValueError):
            c.resume()
        self.assertTrue(c.done)

    def test_nested_coros_separate_state(self):
        def outer():
            inner_log = []
            def inner():
                inner_log.append("inner-1")
                stackweave_c.yield_()
                inner_log.append("inner-2")
            c = stackweave_c.Coro(inner)
            c.resume(); c.resume()
            return inner_log

        c = stackweave_c.Coro(outer)
        c.resume()
        self.assertTrue(c.done)
        self.assertEqual(c.result, ["inner-1", "inner-2"])


class TestScheduler(unittest.TestCase):
    def test_three_fibers_interleave(self):
        log = []
        def worker(name, n):
            for i in range(n):
                log.append((name, i))
                stackweave.yield_()
        stackweave.fiber(worker, "A", 3)
        stackweave.fiber(worker, "B", 3)
        stackweave.fiber(worker, "C", 3)
        stackweave.run(1)
        # Should round-robin A0 B0 C0 A1 B1 C1 ...
        self.assertEqual(log, [
            ("A", 0), ("B", 0), ("C", 0),
            ("A", 1), ("B", 1), ("C", 1),
            ("A", 2), ("B", 2), ("C", 2),
        ])

    def test_sleep_lets_others_run(self):
        log = []
        def sleeper():
            log.append("s1-start")
            stackweave.sleep(0.05)
            log.append("s1-end")
        def burner():
            for i in range(3):
                log.append(("b", i))
                stackweave.yield_()
        stackweave.fiber(sleeper)
        stackweave.fiber(burner)
        t0 = time.monotonic()
        stackweave.run(1)
        elapsed = time.monotonic() - t0
        # burner finished long before sleeper woke
        self.assertEqual(log[:4], ["s1-start", ("b", 0), ("b", 1), ("b", 2)])
        self.assertEqual(log[-1], "s1-end")
        self.assertGreaterEqual(elapsed, 0.04)


if __name__ == "__main__":
    unittest.main()
