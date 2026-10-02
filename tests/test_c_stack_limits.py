"""run(1) and Coro.resume hand their caller back its own C-stack limits.

CPython 3.14 checks for C-stack overflow by comparing the stack pointer with
limits kept on the thread state, and _Py_Dealloc's trashcan measures its margin
against the same limits.  Every resume arms them at the fiber's own stack
(runloom_coro_rearm_stackprot).  The single-thread scheduler and Coro run their
fibers on the caller's thread state and never put the caller's limits back, so
the drain freed objects on its own stack under a fiber's limits, and after
run(1) returned the main thread kept the last fiber's.  Its stack lies above
the fibers', so the check never fired: repr() of a deeply nested list, or
freeing one, ran off the end of the stack (SIGSEGV) instead of raising
RecursionError.

stackweave_c._c_stack_limits() reads the calling thread state's limits.
"""
import functools
import os
import pathlib
import subprocess
import sys

import pytest

import stackweave
import stackweave_c
from adv_util import needs_free_threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
limits = stackweave_c._c_stack_limits


def test_run1_hands_the_caller_its_limits_back():
    before = limits()
    seen = []
    stackweave.run(1, lambda: seen.append(limits()))
    assert seen and seen[0] != before, "the fiber ran under the caller's limits"
    assert limits() == before


def test_the_drain_frees_a_finished_fibers_objects_under_its_own_limits():
    # A fiber's return value is dropped by the drain, on the drain's stack.
    class Probe:
        def __del__(self):
            freed.append(limits())

    def fiber():
        ran.append(limits())
        return Probe()

    freed, ran = [], []
    before = limits()
    stackweave.run(1, fiber)
    assert ran and ran[0] != before, "the fiber ran under the caller's limits"
    assert freed == [before]


def test_a_nested_run1_hands_the_outer_fiber_its_limits_back():
    seen = {}

    def outer():
        seen["outer"] = limits()
        stackweave.run(1, lambda: seen.setdefault("inner", limits()))
        seen["outer after"] = limits()

    stackweave.run(1, outer)
    assert seen["inner"] != seen["outer"]
    assert seen["outer after"] == seen["outer"]


@pytest.mark.skipif(not needs_free_threading(), reason="M:N needs free-threaded CPython")
def test_run2_leaves_the_callers_limits_alone():
    before = limits()
    stackweave.run(2, lambda: None)
    assert limits() == before


def test_coro_resume_hands_the_caller_its_limits_back():
    seen = []

    def body():
        seen.append(limits())
        stackweave_c.yield_()
        seen.append(limits())

    before = limits()
    c = stackweave_c.Coro(body)
    c.resume()                      # parks at yield_
    assert limits() == before
    c.resume()                      # finishes
    assert limits() == before
    assert len(seen) == 2 and seen[0] != before and seen[1] != before


DEEP = """
def deep(n):
    l = []
    for _ in range(n):
        l = [l]
    return l
"""

AFTER = {
    "run(1)": "stackweave.run(1, lambda: None)",
    "Coro.resume": "stackweave_c.Coro(lambda: None).resume()",
    "a run(1) nested in a fiber": None,
}


def _run(code):
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       env=dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src"),
                       capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout + p.stderr[-2000:]


@pytest.mark.parametrize("after", list(AFTER))
def test_deep_recursion_after_a_fiber_raises_instead_of_crashing(after):
    check = ("l = deep(300000)\n"
             "try:\n"
             "    repr(l)\n"
             "    print('no error')\n"
             "except RecursionError:\n"
             "    print('RecursionError')\n")
    if AFTER[after] is None:
        body = ("def outer():\n"
                "    stackweave.run(1, lambda: None)\n"
                + "".join("    " + line + "\n" for line in check.splitlines())
                + "stackweave.run(1, outer)\n")
    else:
        body = AFTER[after] + "\n" + check
    rc, out = _run("import stackweave, stackweave_c\n" + DEEP + body)
    assert rc == 0 and "RecursionError" in out, (rc, out)


def test_freeing_a_deep_structure_after_run1_does_not_crash():
    rc, out = _run("import stackweave\n" + DEEP
                   + "stackweave.run(1, lambda: None)\n"
                   + "l = deep(1000000)\n"
                   + "del l\n"
                   + "print('freed')\n")
    assert rc == 0 and "freed" in out, (rc, out)
