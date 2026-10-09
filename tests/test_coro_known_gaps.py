"""Known gaps of the low-level stackweave_c.Coro API.

Each test states what must hold for a Coro and is a strict xfail until its gap
is closed; then it XPASSes, which fails the run and forces the marker off.
Only a failed behaviour check (an AssertionError) counts as the known break.
A scenario that never reaches its trigger, or dies some other way, calls
pytest.fail, which the marker's raises= does not absorb, so it stays a real
failure.  Each case runs in a subprocess, since most of these crash.

  * A Coro's first frame keeps `previous` pointing at the frame that FIRST
    resumed it.  Once that resumer returns, resuming from anywhere else and
    then raising, walking f_back, calling traceback.extract_stack() or
    gc.collect(), or letting an exception escape, follows the dangling link
    into freed datastack memory.
  * A parked Coro's frames are on no thread's frame chain, and the frames
    anchor (module_gcframes.c.inc) does not visit them either.  A
    gc.collect() while it is parked frees what only their deferred stackrefs
    keep alive (its function and code objects), and the resume runs freed
    bytecode -- the p565 class of bug, for Coro instead of fibers.
  * A plain OS thread that runs Coros exits still holding its cached Coro
    stacks: stats()["coro_stack_live"] stays 2 higher per thread, each a
    512 KB mapping (~520 KB of address space per thread; resident ~32 KB on
    macOS, ~8 KB on Linux).
  * Parking the run(1) fiber from inside a Coro body (a sleep, a channel
    recv) crashes; it must either work or raise.
"""
import os
import pathlib
import signal
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def KNOWN_GAP(reason):
    return pytest.mark.xfail(strict=True, raises=AssertionError,
                             reason="KNOWN BROKEN: " + reason)


def _run(code, timeout=60):
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       env=dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src"),
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _lines(out, tag):
    return [l[len(tag):].strip() for l in out.splitlines() if l.startswith(tag)]


def _fail_unless_crash_is(rc, err, what):
    """A death by a signal other than SIGSEGV/SIGBUS is not the known break."""
    if rc < 0 and -rc not in (signal.SIGSEGV, signal.SIGBUS):
        pytest.fail("%s: killed by signal %d, not the known SIGSEGV/SIGBUS: %s"
                    % (what, -rc, err[-1500:]))


# ---------------------------------------------------------------------------
# The first frame's link to the frame that first resumed the Coro.
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
            tb = e.__traceback__        # keeping the traceback is what crashes
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
    _fail_unless_crash_is(rc, err, mode)
    assert rc == 0, ("resuming the Coro from a second frame crashed it", rc,
                     err[-500:])
    if mode == "escape":
        assert _lines(out, "ESCAPED"), out
    else:
        assert _lines(out, "RESULT") == ["ok"], out
    for chain in _lines(out, "CHAIN"):
        names = chain.split()
        # Whichever fix lands -- re-point the link at each resumer, or root the
        # Coro's chain at NULL -- the frame that first resumed it is gone.
        assert names[0] == "body" and "first_resumer" not in names, names


@pytest.mark.parametrize("mode", FIRST_FRAME_MODES)
def test_a_coro_resumed_from_one_live_frame_walks_its_chain(mode):
    # The control: the module frame that first resumed the Coro is still live
    # when the second resume walks the link.
    rc, out, err = _run(FIRST_FRAME % (mode, True))
    _check_first_frame(mode, rc, out, err)


@KNOWN_GAP("a Coro's first frame keeps `previous` at the frame that first "
           "resumed it; once that frame returns, walking the chain from "
           "another resumer follows a dangling pointer")
@pytest.mark.parametrize("mode", FIRST_FRAME_MODES)
def test_a_coro_resumed_after_its_first_resumer_returned_walks_its_chain(mode):
    rc, out, err = _run(FIRST_FRAME % (mode, False))
    _check_first_frame(mode, rc, out, err)


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
for _ in range(4):
    gc.collect()
print("ALIVE", fn() is not None, code() is not None, flush=True)
fs = []
for i in range(3000):          # reuse whatever the collection freed
    d = {}
    exec("def f(x):\n    return x * %d + %d\n" % (i, i), d)
    fs.append(d["f"])
c.resume()
print("RESULT", c.result, flush=True)
"""


@KNOWN_GAP("a parked Coro's frames are on no frame chain and the frames anchor "
           "does not visit them, so a gc.collect() frees what only their "
           "deferred stackrefs keep alive")
def test_a_parked_coros_frames_keep_their_code_alive_across_a_collection():
    rc, out, err = _run(PARKED_GC)
    if "PARKED" not in out or not _lines(out, "ALIVE"):
        pytest.fail("the Coro never parked or the collection never ran: rc=%s %s"
                    % (rc, err[-1500:]))
    _fail_unless_crash_is(rc, err, "parked Coro")
    assert _lines(out, "ALIVE") == ["True True"], (
        "the parked Coro's function/code were collected", out)
    assert rc == 0 and _lines(out, "RESULT") == ["[42, 'tail-42', 42]"], (
        rc, out, err[-500:])


# ---------------------------------------------------------------------------
# Plain OS threads that run Coros.
# ---------------------------------------------------------------------------

THREADS = r"""
import gc, threading
import stackweave_c
N = %d
ran = []

def work():
    for _ in range(8):
        c = stackweave_c.Coro(lambda: (stackweave_c.yield_(), 1)[1])
        c.resume()
        c.resume()
        ran.append(c.result)

def batch(n):
    for _ in range(n):
        t = threading.Thread(target=work)
        t.start()
        t.join()

def live():
    return stackweave_c.stats()["coro_stack_live"]

batch(20)                       # warm whatever is per-process first
gc.collect()
before = live()
batch(N)
gc.collect()
print("RAN", sum(ran), flush=True)
print("LIVE", before, live(), flush=True)
"""


@KNOWN_GAP("a plain OS thread that ran Coros exits still holding its cached "
           "Coro stacks (2 per thread, each a 512 KB mapping)")
def test_plain_threads_that_ran_coros_release_their_stacks_at_exit():
    n = 200
    rc, out, err = _run(THREADS % n, timeout=120)
    if rc != 0 or _lines(out, "RAN") != [str((n + 20) * 8)]:
        pytest.fail("the threads did not all run their Coros: rc=%s %s %s"
                    % (rc, out, err[-1500:]))
    before, after = map(int, _lines(out, "LIVE")[0].split())
    # coro_stack_live counts stacks in use; every Coro here finished and every
    # thread exited, so nothing should still hold one.
    assert after == before, (
        "%d threads that ran Coros exited holding %d Coro stacks"
        % (n, after - before))


# ---------------------------------------------------------------------------
# Parking the run(1) fiber from inside a Coro body.
# ---------------------------------------------------------------------------

RUN1_PARK = r"""
import stackweave, stackweave_c
MODE = %r
out = []

def main():
    ch = stackweave.Chan(1)
    def feeder():
        stackweave.sleep(0.02)
        ch.send(7)
    stackweave.fiber(feeder)
    def body():
        print("PARKING", flush=True)
        if MODE == "sleep":
            stackweave.sleep(0.01)
            return "slept"
        return ch.recv()[0]
    c = stackweave_c.Coro(body)
    try:
        c.resume()
        while not c.done:
            stackweave.sleep(0.005)
            c.resume()
        out.append("result " + repr(c.result))
    except Exception as e:
        out.append("raised " + type(e).__name__)

stackweave.run(1, main)
print("DONE", *out, flush=True)
"""


@KNOWN_GAP("parking the run(1) fiber from inside a Coro body swaps the fiber "
           "out from under the Coro's stack and crashes")
@pytest.mark.parametrize("mode", ["sleep", "chan_recv"])
def test_parking_the_run1_fiber_inside_a_coro_body_works_or_raises(mode):
    rc, out, err = _run(RUN1_PARK % mode, timeout=30)
    if "PARKING" not in out:
        pytest.fail("the Coro body never ran: rc=%s %s" % (rc, err[-1500:]))
    _fail_unless_crash_is(rc, err, mode)
    # Either outcome is acceptable: the park works, or it raises in the body.
    assert rc == 0 and _lines(out, "DONE"), (
        "parking inside a Coro body crashed", rc, out, err[-500:])
