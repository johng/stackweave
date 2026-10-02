"""A fiber that yields deep in C recursion keeps its stack; copy-grow is off.

At each resume, runloom_coro_maybe_grow used to copy a fiber's stack to one
twice the size once less than a quarter of it was left, rebasing every pointer
INSIDE the copied stack.  The stack is also pointed into from outside: heap
frames whose `previous` is an entry frame on the C stack, the thread state's
critical-section and C-stack-ref chains, and the runtime's own channel,
select and signal waiters.  So a fiber that yielded deep in C recursion
(list(map(...)), each level Python -> C -> Python) SIGSEGVed on its next
resume -- at the default 512 KB stack too, ~155 levels down.  Copy-grow is now
off unless STACKWEAVE_STACK_GROW is set; the fiber keeps its stack and
RecursionError bounds it.

Each fiber parks at EVERY level on the way down and stops at RecursionError.
At 512 KB the trigger sits right at the overflow check's limit, so whether a
yield lands past it depends on the build's frame sizes (they differ ~2.5x
between the stock and patched 3.14t); eight fibers, each first recursing a
different number of levels through another C path, shift where their yields
fall, and some always land in the window.  Each case runs in a subprocess,
since a regression here is a crash.
"""
import os
import pathlib
import signal
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTE = "copy-grow is off"

PRELUDE = """
import sys
import stackweave, stackweave_c
sys.setrecursionlimit(1_000_000)
PADS = 8

def crec(n, park):
    # Parks, then one Python -> C -> Python level: list(map()) calls back into
    # crec from C, so the C stack deepens with n.
    park()
    list(map(lambda k: crec(k, park), [n + 1]))

def pad(j, then):
    # j levels of a differently shaped C recursion first (sorted calls key()
    # from C), shifting where crec's yields fall relative to the trigger.
    if j == 0:
        return then()
    return sorted([0], key=lambda _: pad(j - 1, then))

def down(j, park):
    try:
        pad(j, lambda: crec(0, park))
    except RecursionError:
        pass

def report():
    s = stackweave_c.stats()
    print("DONE", PADS, s["copy_grows"], s["copy_grows_declined"], flush=True)
"""

def _fibers(stack_kw):
    return PRELUDE + """
for j in range(PADS):
    stackweave_c.fiber(lambda j=j: down(j, stackweave_c.sched_yield)%s)
stackweave_c.run()
report()
""" % stack_kw

CASES = {
    "fiber, default stack": _fibers(""),
    "fiber, 1 MB": _fibers(", stack_size=1 << 20"),
    # One Coro at a time: interleaving Coros parked inside list(map())'s
    # critical section is a separate crash (Coro.resume doesn't swap the
    # critical-section chain), not what this tests.
    "Coro, 1 MB": PRELUDE + """
for j in range(PADS):
    c = stackweave_c.Coro(lambda j=j: down(j, stackweave_c.yield_), stack_size=1 << 20)
    while not c.done:
        c.resume()
report()
""",
}


def _run(code, **env):
    full = dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src")
    # Pinned: the knob under test, and the stack arena (with it on, the old
    # stack stays mapped after a grow and a broken grow need not crash).
    full.pop("STACKWEAVE_STACK_GROW", None)
    full.pop("STACKWEAVE_STACK_ARENA", None)
    full.update(env)
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=full,
                       capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


def _done(out):
    """(fibers, grows, declined) from the DONE line, or None."""
    for line in out.splitlines():
        if line.startswith("DONE "):
            return tuple(int(x) for x in line.split()[1:4])
    return None


@pytest.mark.parametrize("case", list(CASES))
def test_a_fiber_yielding_deep_in_c_recursion_keeps_its_stack(case):
    rc, out, err = _run(CASES[case])
    done = _done(out)
    assert rc == 0 and done is not None, (rc, out, err[-2000:])
    fibers, grows, declined = done
    # The trigger was reached (not a vacuous pass), and nothing was copied.
    assert 1 <= declined <= fibers and grows == 0, (done, out)
    assert err.count(NOTE) == 1, err[-2000:]


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="KNOWN BROKEN: copy-grow leaves pointers into the old "
                          "stack and the fiber SIGSEGVs; off unless "
                          "STACKWEAVE_STACK_GROW is set")
@pytest.mark.parametrize("case", list(CASES))
def test_copy_grow_when_turned_on(case):
    rc, out, err = _run(CASES[case], STACKWEAVE_STACK_GROW="1")
    done = _done(out)
    # Anything but "grew and survived" or "killed by a signal" is not this
    # known break, so it fails the test instead of counting as the xfail.
    if NOTE in err:
        pytest.fail("STACKWEAVE_STACK_GROW=1 did not turn copy-grow on: " + err[-500:])
    if rc > 0 or (rc == 0 and (done is None or done[1] == 0)):
        pytest.fail("the run did not exercise copy-grow: rc=%s %r %s"
                    % (rc, out, err[-1000:]))
    if rc < 0 and -rc not in (signal.SIGSEGV, signal.SIGBUS):
        pytest.fail("killed by signal %d, not the known SIGSEGV/SIGBUS: %s"
                    % (-rc, err[-1000:]))
    # The known break: SIGSEGV/SIGBUS once the fiber resumes grown.
    assert rc == 0, ("copy-grow crashed the fiber", rc, out, err[-500:])
