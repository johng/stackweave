"""A stackweave_c.Coro's Python frames: their chain and the GC.

  * A Coro's frames link to whoever resumes it, as a generator's do: each
    resume points its bottom frame at the resumer's frame, and each yield cuts
    that link.  Before, the first frame kept `previous` at the frame that FIRST
    resumed it; once that resumer returned, resuming from anywhere else and
    then raising, walking f_back, calling traceback.extract_stack() or
    gc.collect(), or letting an exception escape, followed the dangling link
    into freed datastack memory and SIGSEGVed.
  * A parked Coro's frames are on no thread's frame chain, so the GC frames
    anchor (module_gcframes.c.inc) visits them.  Before, a gc.collect() while
    it was parked freed what only their deferred stackrefs kept alive (its
    function and code objects), and the resume ran freed bytecode -- the p565
    class of bug, for Coro instead of fibers.

Each case runs in a subprocess, since the old failure was a crash.
"""
import signal
import sys

import pytest

from adv_util import run_python


def _run(code, timeout=60):
    p = run_python(code, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _lines(out, tag):
    return [l[len(tag):].strip() for l in out.splitlines() if l.startswith(tag)]


def _assert_no_crash(rc, err, what):
    assert not (rc < 0 and -rc in (signal.SIGSEGV, signal.SIGBUS)), (
        "%s crashed (signal %d)" % (what, -rc), err[-1500:])


# ---------------------------------------------------------------------------
# The chain links to the current resumer.
# ---------------------------------------------------------------------------

FIRST_FRAME = r"""
import sys, gc, traceback
import stackweave_c
sys.setrecursionlimit(20000)
MODE, FIRST_FROM_MODULE = %r, %r

def body():
    stackweave_c.yield_()
    print("RESUMED", flush=True)
    if MODE == "raise":
        try:
            raise ValueError("inside the coro")
        except ValueError as e:
            tb = e.__traceback__        # keeping the traceback is what crashed
    elif MODE == "f_back":
        f, names = sys._getframe(), []
        while f is not None:
            names.append(f.f_code.co_name)
            f = f.f_back
        print("CHAIN", *names, flush=True)
    elif MODE == "extract_stack":
        print("CHAIN", *reversed([s.name for s in traceback.extract_stack()]), flush=True)
    elif MODE == "gc":
        gc.collect()
    elif MODE == "escape":
        raise ValueError("escapes the coro")
    return "ok"

def first_resumer(n, c):
    # 3000 frames down, so returning frees the datastack chunks they used.
    if n:
        return first_resumer(n - 1, c)
    c.resume()

def churn(n, *pad):
    return churn(n - 1, *pad, n) if n else 0

def second_resumer(c):
    try:
        c.resume()
    except ValueError as e:
        print("ESCAPED", len(traceback.format_exception(e)), flush=True)
        return
    print("RESULT", c.result, flush=True)

c = stackweave_c.Coro(body)
if FIRST_FROM_MODULE:
    c.resume()
else:
    first_resumer(3000, c)
for _ in range(3):
    churn(400)
second_resumer(c)
"""

FIRST_FRAME_MODES = ["raise", "f_back", "extract_stack", "gc", "escape"]


def _check_first_frame(mode, rc, out, err):
    if "RESUMED" not in out.splitlines():
        pytest.fail("the Coro was never resumed a second time: rc=%s %s"
                    % (rc, err[-1500:]))
    _assert_no_crash(rc, err, mode)
    assert rc == 0, (rc, err[-500:])
    if mode == "escape":
        assert _lines(out, "ESCAPED"), out
    else:
        assert _lines(out, "RESULT") == ["ok"], out
    for chain in _lines(out, "CHAIN"):
        # The chain runs through the resumer of THIS resume, never the frame
        # that first resumed it.
        assert chain.split() == ["body", "second_resumer", "<module>"], chain


@pytest.mark.parametrize("mode", FIRST_FRAME_MODES)
def test_a_coro_resumed_from_one_live_frame_walks_its_chain(mode):
    # The control: the module frame that first resumed the Coro is still live
    # when the second resume walks the link.
    rc, out, err = _run(FIRST_FRAME % (mode, True))
    _check_first_frame(mode, rc, out, err)


@pytest.mark.parametrize("mode", FIRST_FRAME_MODES)
def test_a_coro_resumed_after_its_first_resumer_returned_walks_its_chain(mode):
    rc, out, err = _run(FIRST_FRAME % (mode, False))
    _check_first_frame(mode, rc, out, err)


NESTED = r"""
import gc, sys
import stackweave_c
Y = stackweave_c.yield_

def chain():
    f, names = sys._getframe(1), []
    while f is not None:
        names.append(f.f_code.co_name)
        f = f.f_back
    return names

def inner():
    for _ in range(3):
        print("INNER", *chain(), flush=True)
        Y()
    return "inner"

b = stackweave_c.Coro(inner)

def outer():
    b.resume()          # b's first resumer is a frame of Coro a ...
    Y()                 # ... which then parks
    return "outer"

a = stackweave_c.Coro(outer)

def first():
    a.resume()

def second():
    b.resume()          # b resumed from outside a, while a is parked

def third():
    a.resume()

first(); gc.collect()
second(); gc.collect()
third(); gc.collect()
second(); gc.collect()
second()
print("RESULT", a.result, b.result, flush=True)
"""


def test_a_coro_first_resumed_inside_another_coro_follows_its_new_resumer():
    rc, out, err = _run(NESTED)
    _assert_no_crash(rc, err, "nested Coros")
    assert rc == 0, (rc, err[-500:])
    assert _lines(out, "INNER") == ["inner outer first <module>",
                                    "inner second <module>",
                                    "inner second <module>"], out
    assert _lines(out, "RESULT") == ["outer inner"], out


C_CALLABLE = r"""
import functools, gc
import stackweave_c
Y = stackweave_c.yield_

# No Python frame of its own is live when this one yields.
c = stackweave_c.Coro(Y)
c.resume(); gc.collect(); c.resume()
print("BARE", c.done, flush=True)

# A C callable driving a generator that yields the Coro from inside its frame.
def gen():
    for i in range(3):
        Y()
        yield i
c = stackweave_c.Coro(functools.partial(list, gen()))
def resumer():
    c.resume()
while not c.done:
    resumer(); gc.collect()
print("LIST", c.result, flush=True)
"""


def test_a_coro_with_a_c_callable_parks_and_resumes():
    rc, out, err = _run(C_CALLABLE)
    _assert_no_crash(rc, err, "C-callable Coro")
    assert rc == 0, (rc, err[-500:])
    assert _lines(out, "BARE") == ["True"], out
    assert _lines(out, "LIST") == ["[0, 1, 2]"], out


# ---------------------------------------------------------------------------
# A parked Coro's frames and the GC.
# ---------------------------------------------------------------------------

PARKED_GC = r"""
import gc, weakref
import stackweave_c
SRC = '''
import stackweave_c
def inner():
    a = 1
    stackweave_c.yield_()
    b = a + 41
    return [b, "tail-%d" % b, (lambda: b)()]
def outer():
    return inner()
'''
ns = {}
exec(compile(SRC, "<parked>", "exec"), ns)
c = stackweave_c.Coro(ns["outer"])
c.resume()
# inner() is parked: once its name is gone, its frame's deferred stackrefs are
# the only references to the function and its code object.
fn, code = weakref.ref(ns["inner"]), weakref.ref(ns["inner"].__code__)
del ns["inner"]
print("PARKED", flush=True)
walked = stackweave_c.stats()["gc_coros_walked"]
for _ in range(4):
    gc.collect()
print("WALKED", stackweave_c.stats()["gc_coros_walked"] - walked, flush=True)
print("ALIVE", fn() is not None, code() is not None, flush=True)
fs = []
for i in range(3000):          # reuse whatever the collection freed
    d = {}
    exec("def f(x):\n    return x * %d + %d\n" % (i, i), d)
    fs.append(d["f"])
c.resume()
print("RESULT", c.result, flush=True)
"""


def test_a_parked_coros_frames_keep_their_code_alive_across_a_collection():
    rc, out, err = _run(PARKED_GC)
    if "PARKED" not in out or not _lines(out, "ALIVE"):
        pytest.fail("the Coro never parked or the collection never ran: rc=%s %s"
                    % (rc, err[-1500:]))
    # The anchor walked the parked Coro in each collection.
    assert _lines(out, "WALKED") == ["4"], out
    _assert_no_crash(rc, err, "parked Coro")
    assert _lines(out, "ALIVE") == ["True True"], (
        "the parked Coro's function/code were collected", out)
    assert rc == 0 and _lines(out, "RESULT") == ["[42, 'tail-42', 42]"], (
        rc, out, err[-500:])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
