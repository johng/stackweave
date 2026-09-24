"""Cooperative select.select, and a guard on stdlib C-frame footprint.

Background: a fiber runs on a small fixed C stack (default 32 KB, with a
PROT_NONE guard page).  CPython's `select_select_impl` declares three
`pylist[FD_SETSIZE + 1]` arrays -- ~51 KB in a single C frame, the only stdlib
leaf that overflows 32 KB -- so calling it inline in a fiber SEGV'd.  The
fix is NOT a bigger stack: `select.select` is reimplemented cooperatively on a
transient epoll (register the fds, park on the epoll's own fd via netpoll, map
results back), so the fat frame is never allocated on the fiber stack and
the fiber parks like any other socket waiter -- no pool thread, scales.

Two things are tested here:
  * TestCooperativeSelect -- select in a fiber doesn't crash, returns the
    right ready sets, and (the point) stays cooperative: a sibling fiber
    keeps running while one parks in select.
  * TestStdlibFrameFootprint -- a regression guard that measures the C-stack
    high-water mark of the deepest-known stdlib leaves and asserts they fit the
    default fiber stack, so a NEW fat-framed C function (a future stdlib
    addition) that would silently re-arm the SEGV is caught.  select is the
    one allowlisted exception precisely because it's handled cooperatively and
    never runs inline.

SEGV-prone cases run in a child interpreter and assert a clean exit (rc 0);
rc -11 (SIGSEGV) = the regression is back.
"""
import os
import re as _re
import subprocess
import sys
import unittest

import stackweave_c

import os as _hwm_os
import pytest as _hwm_pytest
# Stack high-water-mark is precise only with 4 KB pages: macOS 16 KB pages make
# the mincore-based HWM over-report (it reports the whole stack resident), so
# these HWM/advice/sizing tests can't measure precisely there -- skip them (the
# diagnostic itself just over-reserves, which is safe).
_RELIABLE_HWM = _hwm_os.sysconf("SC_PAGESIZE") == 4096
pytestmark = _hwm_pytest.mark.skipif(
    not _RELIABLE_HWM,
    reason="stack HWM is reliable only with 4 KB pages")

# The static _RELIABLE_HWM gate above predicts the "reports the whole stack
# resident" failure from the page size.  Hosted CI runners hit it
# anyway, with 4 KB pages: mincore reports which pages are RESIDENT, and that
# equals "touched" only while the host is not under memory pressure.  On a
# loaded shared runner the entire fiber stack can read resident and the probe
# then returns the ALLOCATION for every measurement -- 2 MiB here, whatever the
# frame really used.  Observed on both ubuntu-latest legs, where all three
# footprint assertions failed with the identical value 2097152: three different
# frames cannot all be exactly 2 MiB, so that is the probe failing, not stackweave.
#
# Rather than predict it, ASK: measure a fiber whose body is `pass`.  It cannot
# have touched 2 MiB.  If the probe says it did, every number this class
# produces is the allocation size and none of the assertions mean anything.
_HWM_SENTINEL = None   # None = not probed yet; True = probe is unusable here

# The stack the probe subprocess allocates for the measured fiber.  A reading
# at or above this IS the allocation, i.e. the probe failed -- see _measure_hwm.
_PROBE_STACK = 2 * 1024 * 1024


def _hwm_probe_untrustworthy():
    global _HWM_SENTINEL
    if _HWM_SENTINEL is None:
        stack = 2 * 1024 * 1024
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import stackweave_c\n"
            "def worker():\n"
            "    pass\n"
            "stackweave_c.fiber(worker, stack_size=%d)\n"
            "stackweave_c.run()\n"
            "print('HWM', stackweave_c.stats().get('stack_hwm', 0))\n"
            % (os.path.join(REPO, "src"), stack)
        )
        env = dict(os.environ, PYTHON_GIL="0", STACKWEAVE_GIL="0")
        try:
            p = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                               timeout=60, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
            m = _re.search(r"HWM (\d+)", p.stdout or "")
            # Half the allocation is a deliberately loose bar: a do-nothing
            # fiber uses a few KB, so anything near the allocation means
            # residency, not usage.  Loose so a genuinely fat frame is never
            # mistaken for a broken probe.
            _HWM_SENTINEL = bool(m) and int(m.group(1)) > stack // 2
        except Exception:
            _HWM_SENTINEL = False   # cannot tell -> assume usable, let it fail loudly
    return _HWM_SENTINEL

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_child(code, timeout=60):
    preamble = (
        "import sys; sys.path.insert(0, %r)\n"
        "import stackweave, stackweave_c\n"
        "stackweave.monkey.patch()\n" % os.path.join(REPO, "src")
    )
    env = dict(os.environ)
    env["PYTHON_GIL"] = "0"
    env["STACKWEAVE_GIL"] = "0"
    try:
        p = subprocess.run(
            [sys.executable, "-c", preamble + code],
            cwd=REPO, env=env, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except subprocess.TimeoutExpired:
        return 124, "", "[timed out]"
    return p.returncode, p.stdout, p.stderr


def assert_pass(code, **kw):
    rc, out, err = run_child(code, **kw)
    sig = "  (SIGSEGV -- the stack-overflow regression is back)" if rc == -11 else ""
    assert rc == 0 and "PASS" in out, (
        "rc={0}{1}\n--- stdout ---\n{2}\n--- stderr ---\n{3}".format(
            rc, sig, out, err))
    return out


class TestCooperativeSelect(unittest.TestCase):
    def test_no_segv_empty_select_m1(self):
        # The original crash: select([],[],[],0) inline overflowed 32 KB.
        assert_pass(r"""
import select
def w():
    for _ in range(50):
        select.select([], [], [], 0)
stackweave_c.fiber(w); stackweave_c.run()
print("PASS")
""")

    def test_no_segv_select_mn(self):
        assert_pass(r"""
import select, socket
pairs = [socket.socketpair() for _ in range(3)]
def w():
    for _ in range(30):
        select.select([a for a, b in pairs], [], [], 0)
        stackweave.sleep(0.0001)
stackweave.mn_init(2)
for _ in range(6):
    stackweave_c.mn_fiber(w)
stackweave.mn_run(); stackweave.mn_fini()
print("PASS")
""")

    def test_returns_readable(self):
        assert_pass(r"""
import select, socket
a, b = socket.socketpair()
def w():
    b.sendall(b"x")
    r, wl, x = select.select([a], [], [], 1.0)
    assert r == [a], (r, wl, x)
    assert a.recv(1) == b"x"
print("READY")  # marker before
stackweave_c.fiber(w); stackweave_c.run()
print("PASS")
""")

    def test_returns_writable(self):
        assert_pass(r"""
import select, socket
a, b = socket.socketpair()
def w():
    r, wl, x = select.select([], [a], [], 1.0)
    assert wl == [a], (r, wl, x)
stackweave_c.fiber(w); stackweave_c.run()
print("PASS")
""")

    def test_timeout_returns_empty(self):
        # A fd that never becomes readable: select must time out cleanly.
        assert_pass(r"""
import select, socket, time
a, b = socket.socketpair()
def w():
    t0 = time.monotonic()
    r, wl, x = select.select([a], [], [], 0.2)
    dt = time.monotonic() - t0
    assert (r, wl, x) == ([], [], []), (r, wl, x)
    assert dt >= 0.15, dt
stackweave_c.fiber(w); stackweave_c.run()
print("PASS")
""")

    def test_stays_cooperative_sibling_runs(self):
        # THE point: while one fiber parks in select, a sibling keeps
        # running on the same hub.  If select wedged the hub (blocking inline
        # or busy-poll), the canary would barely tick.  M:1 (one thread) is the
        # strictest check.
        out = assert_pass(r"""
import select, socket, stackweave_c
a, b = socket.socketpair()
ticks = [0]
def canary():
    for _ in range(40):
        stackweave_c.sched_sleep(0.01)
        ticks[0] += 1
def waiter():
    r, wl, x = select.select([a], [], [], 0.3)   # never readable -> parks 0.3s
    assert (r, wl, x) == ([], [], []), (r, wl, x)
stackweave_c.fiber(canary)
stackweave_c.fiber(waiter)
stackweave_c.run()
assert ticks[0] >= 10, ticks[0]   # cooperative: canary ran while waiter parked
print("PASS ticks=%d" % ticks[0])
""")
        m = _re.search(r"ticks=(\d+)", out)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 10)


class TestStdlibFrameFootprint(unittest.TestCase):
    """Measure the C-stack high-water mark of the deepest-known stdlib leaves
    and assert they fit the default fiber stack.  Catches a NEW fat-framed
    C function before it can re-arm the guard-page SEGV."""

    # Raw (unpatched) C-stack high-water marks, free-threaded 3.13t:
    #   select.select        50.9 KB  -- the FD_SETSIZE arrays (handled: cooperative)
    #   first ssl use        ~40   KB  -- OpenSSL one-time init (handled: main-thread warm)
    #   json (nested)         6.3 KB
    #   getaddrinfo / re      ~2.7 KB
    # Two fat frames exist (select, first-ssl); both have mitigations asserted
    # by their own tests below.  Everything else must fit the default stack.
    LEAVES = {
        "getaddrinfo": "import socket; socket.getaddrinfo('127.0.0.1', 80)",
        "json":        "import json; json.loads(json.dumps({'a':[1,2,{'b':3}]*50}))",
        "re":          "import re; re.match(r'(a|b)*c', 'ab'*40 + 'c')",
    }

    def _measure_hwm(self, op_src):
        # Refuse to assert on a probe that is reporting residency (see
        # _hwm_probe_untrustworthy).  Skipping a MEASUREMENT we know is invalid
        # is not the same as skipping the test because it is inconvenient: with
        # a broken probe the assertion below cannot fail for a real reason.
        if _hwm_probe_untrustworthy():
            self.skipTest("stack HWM probe is reporting the whole allocation "
                          "(mincore residency under memory pressure) -- the "
                          "measurement is meaningless here, not failing")
        # Measure the RAW stdlib leaf (NO monkey.patch): the guard is about the
        # C-frame footprint of the unpatched function -- that's what determines
        # whether it needs a cooperative path.  A roomy 2 MB stack so the fat
        # frame can't crash the measurement.
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import stackweave_c\n"
            "def worker():\n"
            "    %s\n"
            "stackweave_c.fiber(worker, stack_size=%d)\n"
            "stackweave_c.run()\n"
            "print('HWM', stackweave_c.stats().get('stack_hwm', 0))\n"
            % (os.path.join(REPO, "src"), op_src, _PROBE_STACK)
        )
        env = dict(os.environ, PYTHON_GIL="0", STACKWEAVE_GIL="0")
        p = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                           timeout=60, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        m = _re.search(r"HWM (\d+)", p.stdout)
        self.assertIsNotNone(m, p.stdout)
        hwm = int(m.group(1))
        # VALIDATE THIS MEASUREMENT, not a proxy for it.  The out-of-band
        # sentinel above probes a SEPARATE, quiet subprocess whose body is
        # `pass`, and caches the verdict for the session.  If that probe happens
        # to run while the host is not under memory pressure it reports clean and
        # the class proceeds -- while these measurements, taken later and under
        # load, get the residency over-report anyway.  That is exactly what
        # happened on ubuntu-latest 3.14.4 (run 33962626563): the sentinel did
        # not fire and all three assertions failed with the identical value
        # 2097152, the full allocation.  The same "sentinel measured a quieter
        # moment than the thing it guards" flaw was already fixed once, in
        # test_stack_grow_down.
        #
        # A frame cannot have touched the WHOLE 2 MB stack we just allocated for
        # it, so a reading at or above the allocation is the probe returning the
        # allocation.  Checking the actual number costs nothing and cannot be
        # fooled by timing.
        if hwm >= _PROBE_STACK:
            self.skipTest(
                "stack HWM probe returned the whole {0} B allocation ({1} B) -- "
                "mincore is reporting residency, not touched pages, so this "
                "measurement is meaningless (not a stackweave failure)"
                .format(_PROBE_STACK, hwm))
        return hwm

    def test_leaf_frames_fit_default_stack(self):
        default = stackweave_c.get_stack_size()
        # Leave headroom for the Python/user frames stacked above the leaf.
        budget = int(default * 0.6)
        for name, src in self.LEAVES.items():
            hwm = self._measure_hwm(src)
            self.assertLess(
                hwm, budget,
                "{0} uses {1} B of C stack (> {2} B budget of the {3} B default "
                "fiber stack); it needs a cooperative path or an allowlist "
                "entry, like select.select".format(name, hwm, budget, default))

    def test_select_is_the_known_fat_frame(self):
        # select.select's raw frame is the fattest stdlib single frame (~51 KB:
        # three pylist[FD_SETSIZE+1] arrays).  At the 512 KB default it FITS, so
        # the cooperative path (monkey/polling.py) is no longer needed to avoid
        # an overflow -- it stays because it makes select PARK on netpoll instead
        # of blocking the hub.  Still assert it's the known-fat frame so a CPython
        # change is noticed; and that it now fits the default.
        hwm = self._measure_hwm("import select; select.select([], [], [], 0)")
        default = stackweave_c.get_stack_size()
        self.assertGreater(hwm, 32 * 1024,
            "select's frame ({0} B) is no longer fat; re-check the measurement"
            .format(hwm))
        self.assertLess(hwm, default,
            "select's frame ({0} B) exceeds the {1} B default -- it would need a "
            "bigger default or stay an overflow risk".format(hwm, default))

    def test_first_ssl_use_is_fat(self):
        # The OTHER fat frame: the first _ssl use drives a ~40 KB OpenSSL init.
        # At the 512 KB default it fits, so ssl-warming-on-main-thread is no
        # longer needed to avoid an overflow -- it stays as a cheap one-time init
        # prepay.  Documented/measured here so it isn't forgotten.
        hwm = self._measure_hwm(
            "import ssl; ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)")
        default = stackweave_c.get_stack_size()
        self.assertGreater(hwm, 32 * 1024,
            "first ssl use ({0} B) is no longer fat; re-check".format(hwm))
        self.assertLess(hwm, default,
            "first ssl use ({0} B) exceeds the {1} B default".format(hwm, default))

    def test_ssl_warmed_on_main_thread_so_fiber_is_safe(self):
        # Mitigation: stackweave.monkey imports ssl on the main thread and
        # _patch_ssl forces OpenSSL init there, off any fiber stack.  So a
        # fiber that is the first to create an SSLContext must NOT crash.
        # (Guard against a future refactor that lazy-imports ssl -> re-arms the
        # 40 KB init on a 32 KB fiber stack.)
        assert_pass(r"""
import ssl
def w():
    ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)   # first context, on a fiber
stackweave_c.fiber(w); stackweave_c.run()
print("PASS")
""")


class TestDeepRecursionSafety(unittest.TestCase):
    """Deeply-nested input to C-recursive stdlib ops must not SEGV a fiber.

    Two mechanisms keep it safe:
      * json/pickle/marshal/copy.deepcopy (~60-80 B of C stack per level)
        degrade to a clean RecursionError -- CPython's recursion counter fires
        (~150 levels ~ 12 KB) well within the 32 KB default stack.
      * ast/compile (~1.5 KB per level, which WOULD SEGV past ~18 deep before
        the counter fires) are auto-offloaded to the backend pool's full-size
        thread stack when called inside a fiber (the `compile` patch).
    eval(str)/exec(str) compile internally in C (not via builtins.compile) and
    are the documented residual -- use offload()/a roomier g-stack.
    """

    def test_json_bomb_is_clean_recursionerror(self):
        assert_pass(r"""
import json
def w():
    try:
        json.loads("[" * 5000 + "]" * 5000)
        ok = "no-error"
    except RecursionError:
        ok = "clean"
    assert ok == "clean", ok
stackweave_c.fiber(w); stackweave_c.run()
print("PASS")
""")

    def test_pickle_deep_is_clean(self):
        assert_pass(r"""
import pickle
def w():
    # build the nesting in-fiber (pure-Python loop -> datastack, safe);
    # pickle.dumps then C-recurses and must hit a clean RecursionError, not SEGV.
    x = []; cur = x
    for _ in range(5000):
        n = []; cur.append(n); cur = n
    try:
        pickle.loads(pickle.dumps(x))
        ok = "no-error"
    except RecursionError:
        ok = "clean"
    assert ok == "clean", ok
stackweave_c.fiber(w); stackweave_c.run()
print("PASS")
""")

    def test_compile_deep_offloaded_no_segv(self):
        # compile of 100-deep nested source SEGVs inline (~1.5 KB/level) but is
        # auto-offloaded to the pool's 8 MB stack by the `compile` patch.
        assert_pass(r"""
SRC = "(" * 100 + "1" + ")" * 100
def w():
    code = compile(SRC, "<s>", "eval")   # auto-offloaded inside a fiber
    assert eval(code) == 1
stackweave_c.fiber(w); stackweave_c.run()
print("PASS")
""")

    def test_ast_parse_deep_offloaded_no_segv(self):
        # ast.parse routes through builtins.compile, so it's covered too.
        assert_pass(r"""
import ast
SRC = "(" * 100 + "1" + ")" * 100
def w():
    tree = ast.parse(SRC)
    assert type(tree).__name__ == "Module"
stackweave_c.fiber(w); stackweave_c.run()
print("PASS")
""")

    def test_compile_passthrough_off_fiber(self):
        # Off any fiber, compile must be the plain builtin (no offload).
        assert_pass(r"""
assert eval(compile("6*7", "<s>", "eval")) == 42
print("PASS")
""")


if __name__ == "__main__":
    unittest.main()
