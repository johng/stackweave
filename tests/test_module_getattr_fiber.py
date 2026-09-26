"""Regression: a module attribute MISS inside a fiber must raise a clean
AttributeError, never crash.

On a miss, CPython's module getattr calls _PyModule_IsPossiblyShadowing to
append a "did you shadow a stdlib module?" hint to the AttributeError.  That
helper reserves two wchar_t[MAXPATHLEN] path buffers on the C stack (~32 KB on
Linux, ~8 KB on macOS).  When fibers ran 3.13 on stacks of 32 KB or less, an
ordinary miss (hasattr / getattr feature-detection, a namespace __getattr__
proxy) overflowed and SIGSEGV'd, so stackweave replaced PyModule_Type's getattr
slot to skip the hint on a fiber.

stackweave now requires free-threaded CPython 3.14+, where every fiber stack is
at least 256 KB and CPython's stack-overflow check is armed 96 KB short of its
end, so the replacement is gone and these tests pin that CPython's own lookup
is fiber-safe -- including at the deepest point a minimum-size fiber reaches.
"""
import os
import subprocess
import sys
import tempfile
import types
import unittest

import stackweave_c

MODNAME = "runloom_modmiss_mod"


def _drive(fn):
    """Run fn() inside a single-thread fiber; re-raise anything it raised."""
    box = [None, None]

    def runner():
        try:
            box[0] = fn()
        except BaseException as e:   # noqa: BLE001
            box[1] = e

    stackweave_c.fiber(runner)
    stackweave_c.run()
    if box[1] is not None:
        raise box[1]
    return box[0]


class TestModuleGetattrGoroutine(unittest.TestCase):
    def setUp(self):
        # A module WITH a __file__ is what makes CPython attempt the 32 KB
        # shadowing hint on a miss (the path that overflows the fiber stack).
        fd, self.path = tempfile.mkstemp(suffix=".py", prefix="runloom_modmiss_")
        os.close(fd)
        self.mod = types.ModuleType(MODNAME)
        self.mod.__file__ = self.path
        self.mod.present = 123
        sys.modules[MODNAME] = self.mod

    def tearDown(self):
        sys.modules.pop(MODNAME, None)
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_miss_in_fiber_raises_attributeerror(self):
        def body():
            with self.assertRaises(AttributeError):
                getattr(self.mod, "definitely_missing")
            return "ok"
        self.assertEqual(_drive(body), "ok")

    def test_hit_in_fiber_still_works(self):
        self.assertEqual(_drive(lambda: getattr(self.mod, "present")), 123)

    def test_hasattr_miss_in_fiber(self):
        self.assertIs(_drive(lambda: hasattr(self.mod, "nope")), False)

    def test_module_level_getattr_function_honoured(self):
        # PEP 562 module __getattr__ must still be called on a miss (in-fiber).
        seen = []

        def mod_getattr(name):
            seen.append(name)
            if name == "magic":
                return "conjured"
            raise AttributeError(name)

        self.mod.__getattr__ = mod_getattr
        self.assertEqual(_drive(lambda: getattr(self.mod, "magic")), "conjured")
        with self.assertRaises(AttributeError):
            _drive(lambda: getattr(self.mod, "still_missing"))
        self.assertIn("magic", seen)

    def test_miss_under_mn_scheduler(self):
        box = [None]

        def runner():
            try:
                getattr(self.mod, "missing_mn")
                box[0] = "NO ERROR"
            except AttributeError:
                box[0] = "ok"

        stackweave_c.mn_init(2)
        try:
            stackweave_c.mn_fiber(runner)
            stackweave_c.mn_run()
        finally:
            stackweave_c.mn_fini()
        self.assertEqual(box[0], "ok")

    def test_subclass_getattr_miss(self):
        # A ModuleType subclass with a class-level __getattr__ reaches stock
        # module getattr via slot_tp_getattr_hook -> the __getattribute__ wrapper
        # descriptor, NOT the tp_getattro slot.  Must still be safe.
        class _NS(types.ModuleType):
            def __getattr__(self, name):
                raise AttributeError(name)

        m = _NS("runloom_modmiss_subcls")
        m.__file__ = self.path

        def body():
            with self.assertRaises(AttributeError):
                m.definitely_missing
            return "ok"
        self.assertEqual(_drive(body), "ok")

    def test_explicit_getattribute_miss(self):
        # Calling module.__getattribute__('missing') directly also goes through
        # the wrapper descriptor, bypassing the slot.  Must still be safe.
        def body():
            with self.assertRaises(AttributeError):
                type(self.mod).__getattribute__(self.mod, "definitely_missing")
            return "ok"
        self.assertEqual(_drive(body), "ok")


_NEAR_LIMIT_CHILD = r"""
import os, sys, tempfile
import stackweave_c as s
d = tempfile.mkdtemp()
sys.path.insert(0, d)
with open(os.path.join(d, "sw_nearlimit_target.py"), "w") as f:
    f.write("X = 1\n")
import sw_nearlimit_target as mod    # file-backed: a miss runs the shadowing check
out = {}

def rec(n):
    out["depth"] = n
    try:
        mod.definitely_missing           # LOAD_ATTR -> the unsuppressed miss path
    except AttributeError as e:
        out["msg"] = str(e)
    return sorted([0], key=lambda _: rec(n + 1))   # C-level recursion per level

def body():
    try:
        rec(0)
    except RecursionError:
        out["end"] = "RecursionError"

if sys.argv[1] == "single":
    s.fiber(body, MIN_STACK)
    s.run()
else:
    s.mn_init(2)
    try:
        s.mn_fiber(body, MIN_STACK)
        s.mn_run()
    finally:
        s.mn_fini()
print("END", out.get("end"), out["depth"], out["msg"])
""".replace("MIN_STACK", str(256 * 1024))


class TestModuleMissNearStackLimit(unittest.TestCase):
    """A miss at the deepest point a minimum-size (256 KB) fiber can reach,
    i.e. with CPython's overflow check just short of firing: the hint's
    buffers must still fit.  In a subprocess, so a crash fails the test
    instead of killing the runner."""

    def _run(self, mode):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, PYTHON_GIL="0",
                   PYTHONPATH=os.path.join(repo, "src"))
        p = subprocess.run([sys.executable, "-c", _NEAR_LIMIT_CHILD, mode],
                           env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0,
                         "child died (rc=%d)\n%s" % (p.returncode, p.stderr[-2000:]))
        end, depth, msg = p.stdout.split(None, 3)[1:]
        self.assertEqual(end, "RecursionError")
        self.assertGreater(int(depth), 5)
        # CPython's own message, i.e. the stock lookup (with its hint check) ran.
        self.assertIn("module 'sw_nearlimit_target' has no attribute", msg)

    def test_single_thread(self):
        self._run("single")

    def test_mn(self):
        self._run("mn")


if __name__ == "__main__":
    unittest.main()
