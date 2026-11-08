"""Preemption through the interpreter's eval-breaker fiber hook.

sysmon preempts a fiber that holds its hub past the time slice.  It used to
reach the fiber through a PEP 523 eval-frame function, installed for the whole
process at the first mn_init.  That function's mere presence makes CPython
deoptimize every specialized Python-to-Python call: each call re-enters
_PyEval_EvalFrameDefault on the C stack, so calls ran ~1.8x slower -- in fibers
and in plain threads alike -- and recursion in a fiber's 512 KB stack stopped
at ~180 frames (~2 KB of C stack per frame) instead of at the recursion limit.

The exec-home patch adds a fiber hook at the end of _Py_HandlePending
(_PyEval_SetFiberBreakerHook / _PyEval_RequestFiberBreak, src/patches).  Where
the interpreter has it, preemption registers that hook and installs no
eval-frame function: sysmon raises the running fiber's eval breaker, and the
hook yields it at its next function entry or loop back-edge.

Recursion depth is the deterministic witness that no eval-frame function is
installed: with one, a fiber cannot recurse a few hundred frames.  An
interpreter built from an older copy of src/patches has no hook and keeps the
eval-frame function; there these are known gaps (rebuild the interpreter).
"""
import sys

import pytest

import stackweave_c

from adv_util import run_python
from known_gaps import KNOWN_GAP

NEEDS_HOOK = KNOWN_GAP(
    "this interpreter predates the exec-home patch's eval-breaker fiber hook "
    "(rebuild it from src/patches): preemption falls back to a PEP 523 "
    "eval-frame function, which deoptimizes every Python call and caps fiber "
    "recursion near 180 frames",
    raises=AssertionError)

pytestmark = [] if stackweave_c.fiber_breaker_hook else [NEEDS_HOOK]


def _assert_pass(code, timeout=60):
    p = run_python(code, timeout=timeout)
    assert p.returncode == 0 and "PASS" in p.stdout, (
        p.returncode, p.stdout[-2000:], p.stderr[-4000:])
    return p.stdout


def test_fibers_recurse_to_the_recursion_limit():
    # 3000 Python frames need no C stack with specialized calls; through an
    # eval-frame function they need ~6 MB, twelve times a fiber's stack.
    _assert_pass(r"""
import sys
import stackweave, stackweave_c
sys.setrecursionlimit(5000)
def down(n):
    return 0 if n == 0 else 1 + down(n - 1)
out = []
stackweave.run(2, lambda: out.append(down(3000)))
assert out == [3000], out
st = stackweave_c.stats()
assert st["preempt_via_breaker"] == 1, st["preempt_via_breaker"]
print("PASS")
""")


def test_plain_threads_keep_specialized_calls_after_run():
    # The eval-frame function was process-wide and never removed: once run()
    # had installed it, a plain thread's recursion was C-stack bound too.
    _assert_pass(r"""
import sys, threading
import stackweave
sys.setrecursionlimit(60000)
stackweave.run(2, lambda: None)
def down(n):
    return 0 if n == 0 else 1 + down(n - 1)
out = []
t = threading.Thread(target=lambda: out.append(down(50000)))
t.start(); t.join()
assert out == [50000], out
print("PASS")
""")


def test_three_single_frame_loops_on_one_hub_all_take_turns():
    # Without the hook a single-frame loop is preempted from a pending call,
    # and the preempted fiber keeps the interpreter's pending-call slot until
    # it resumes, so the next one on the hub ran to its end.  Each fiber here
    # spins in one frame for 30 steps of 10 ms and records the turn order.
    out = _assert_pass(r"""
import time
import stackweave_c
seq = []
def hog(name):
    for _ in range(30):
        end = time.perf_counter() + 0.01
        while time.perf_counter() < end:
            pass
        if not seq or seq[-1] != name:
            seq.append(name)
stackweave_c.mn_init(2)
for name in "ABC":
    stackweave_c.mn_fiber(lambda name=name: hog(name), hub=0)
stackweave_c.mn_run()
stackweave_c.mn_fini()
turns = "".join(seq)
print("turns", turns)
# every fiber was switched out mid-run at least once: a fiber that ran to its
# end in one turn appears in the sequence exactly once
assert all(turns.count(c) >= 2 for c in "ABC"), turns
st = stackweave_c.stats()
assert st["preempt_via_breaker"] == 1 and st["preempt_yields"] >= 3, st
print("PASS")
""", timeout=30)
    assert "turns" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
