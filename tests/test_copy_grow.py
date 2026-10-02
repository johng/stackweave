"""A fiber that yields deep in C recursion keeps its stack; copy-grow is off.

At each resume, runloom_coro_maybe_grow used to copy a fiber's stack to one
twice the size once less than a quarter of it was left, rebasing every pointer
INSIDE the copied stack.  On CPython 3.14 the stack is also pointed into from
outside: the thread state's current_frame, critical_section and c_stack_refs,
and heap frames whose `previous` is an entry frame on the C stack.  So a fiber
given a 1 MB stack that recursed ~330 levels through C (list(map(...)), each
level Python -> C -> Python) and yielded at the bottom SIGSEGVed on its next
resume.  Copy-grow is now off unless STACKWEAVE_STACK_GROW=1; the fiber keeps
its stack, and RecursionError bounds it as on any other fiber.  Stacks up to
512 KB don't reach the trigger: their overflow check trips at or above its
depth.

Each case runs in a subprocess, since a regression here is a crash.
"""
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTE = "copy-grow is off"

PRELUDE = """
import sys
import stackweave, stackweave_c
sys.setrecursionlimit(1_000_000)
DEPTH = 330

def crec(n, park):
    # One Python -> C -> Python level per n: list(map()) calls back into crec
    # from C, so the C stack deepens with n.  Parks at the bottom.
    if n == 0:
        park()
        return 0
    return 1 + list(map(lambda k: crec(k, park), [n - 1]))[0]
"""

FIBER = PRELUDE + """
got = []
def deep():
    got.append(crec(DEPTH, stackweave_c.sched_yield))
def spin():
    for _ in range(5):
        stackweave_c.sched_yield()
stackweave_c.fiber(deep, stack_size=1 << 20)
stackweave_c.fiber(spin)
stackweave_c.run()
s = stackweave_c.stats()
print("DONE", got, s["copy_grows"], s["copy_grows_declined"], flush=True)
"""

CORO = PRELUDE + """
got = []
c = stackweave_c.Coro(lambda: got.append(crec(DEPTH, stackweave_c.yield_)),
                      stack_size=1 << 20)
while not c.done:
    c.resume()
s = stackweave_c.stats()
print("DONE", got, s["copy_grows"], s["copy_grows_declined"], flush=True)
"""


def _run(code, **env):
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       env=dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src", **env),
                       capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


@pytest.mark.parametrize("code", [FIBER, CORO], ids=["fiber", "Coro"])
def test_a_fiber_yielding_deep_in_c_recursion_keeps_its_stack(code):
    rc, out, err = _run(code)
    assert rc == 0 and "DONE [330]" in out, (rc, out, err[-2000:])
    grows, declined = map(int, out.split("DONE [330]", 1)[1].split()[:2])
    # The trigger was reached (not a vacuous pass), and nothing was copied.
    assert declined >= 1 and grows == 0, (grows, declined, out)
    assert err.count(NOTE) == 1, err[-2000:]


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="KNOWN BROKEN: copy-grow leaves pointers into the old "
                          "stack on CPython 3.14 and the fiber SIGSEGVs; off "
                          "unless STACKWEAVE_STACK_GROW=1")
@pytest.mark.parametrize("code", [FIBER, CORO], ids=["fiber", "Coro"])
def test_copy_grow_when_turned_on(code):
    rc, out, err = _run(code, STACKWEAVE_STACK_GROW="1")
    if NOTE in err:
        pytest.fail("STACKWEAVE_STACK_GROW=1 did not turn copy-grow on: " + err[-500:])
    # Turned on, the grow must happen and the fiber must survive it.
    assert rc == 0 and "DONE [330]" in out, (rc, out, err[-2000:])
    grows = int(out.split("DONE [330]", 1)[1].split()[0])
    assert grows >= 1, out
