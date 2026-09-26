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



@unittest.skipUnless(sys.version_info >= (3, 15), "PEP 810 lazy imports are 3.15+")
class TestModuleGetattrLazyImports(unittest.TestCase):
    """3.15 resolves PEP 810 lazy imports inside CPython's module getattr.  While
    stackweave replaced that slot, `mod.name` for a `lazy from` binding came back
    as the raw lazy_import proxy -- e.g. concurrent.futures.ThreadPoolExecutor was
    not callable -- from the moment stackweave_c was imported.  Pin that lazy
    bindings and pending lazy submodules resolve, in and out of fibers.

    The `lazy` statements live in generated modules so this file still parses on
    3.14."""

    _seq = 0

    def setUp(self):
        TestModuleGetattrLazyImports._seq += 1
        self.tag = "sw_lazy_%d_%d" % (os.getpid(), self._seq)
        self.dir = tempfile.mkdtemp(prefix="sw_lazy_")
        sys.path.insert(0, self.dir)

    def tearDown(self):
        sys.path.remove(self.dir)
        for name in [n for n in sys.modules if n.startswith(self.tag)]:
            del sys.modules[name]

    def _write(self, relpath, src):
        path = os.path.join(self.dir, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(src)

    def _lazy_from_module(self):
        # The target must not be imported yet, or `lazy from` binds eagerly.
        target = self.tag + "_target"
        self._write(target + ".py", "def fn():\n    return 'real'\n")
        name = self.tag + "_host"
        self._write(name + ".py", "lazy from %s import fn\n" % target)
        mod = __import__(name)
        # Precondition: the binding really is still lazy before we look.
        self.assertEqual(type(mod.__dict__["fn"]).__name__, "lazy_import")
        return mod

    def test_lazy_binding_resolves_off_fiber(self):
        mod = self._lazy_from_module()
        self.assertEqual(mod.fn(), "real")
        self.assertEqual(type(mod.__dict__["fn"]).__name__, "function")  # stored

    def test_lazy_binding_resolves_in_fiber(self):
        mod = self._lazy_from_module()
        self.assertEqual(_drive(lambda: mod.fn()), "real")
        self.assertEqual(type(mod.__dict__["fn"]).__name__, "function")

    def test_lazy_binding_resolves_via_getattribute_descriptor(self):
        mod = self._lazy_from_module()
        got = _drive(lambda: type(mod).__getattribute__(mod, "fn"))
        self.assertEqual(got(), "real")

    def test_pending_lazy_submodule_loads_in_fiber(self):
        # `lazy import pkg.sub` leaves `sub` pending on pkg: stock getattr loads
        # it on first access even through a separately imported `pkg`.  In a
        # fiber that access is a miss, which the slot used to turn straight into
        # an AttributeError.
        pkg = self.tag + "_pkg"
        self._write(os.path.join(pkg, "__init__.py"), "")
        self._write(os.path.join(pkg, "sub.py"), "VALUE = 42\n")
        self._write(self.tag + "_subhost.py", "lazy import %s.sub\n" % pkg)
        __import__(self.tag + "_subhost")
        self.assertNotIn(pkg + ".sub", sys.modules)

        def body():
            p = __import__(pkg)
            return p.sub.VALUE
        self.assertEqual(_drive(body), 42)

    def test_miss_on_lazy_module_in_fiber_raises_attributeerror(self):
        mod = self._lazy_from_module()

        def body():
            with self.assertRaises(AttributeError):
                mod.definitely_missing
            return "ok"
        self.assertEqual(_drive(body), "ok")


if __name__ == "__main__":
    unittest.main()
