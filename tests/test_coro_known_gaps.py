"""Known gaps of the low-level stackweave_c.Coro API.

Each test states what must hold for a Coro and is a strict xfail until its gap
is closed; then it XPASSes, which fails the run and forces the marker off.
Only a failed behaviour check (an AssertionError) counts as the known break.
A scenario that never reaches its trigger, or dies some other way, calls
pytest.fail, which the marker's raises= does not absorb, so it stays a real
failure.  Each case runs in a subprocess, since most of these crash.

  * A plain OS thread that runs Coros exits still holding its cached Coro
    stacks: stats()["coro_stack_live"] stays 2 higher per thread, each a
    512 KB mapping (~520 KB of address space per thread; resident ~32 KB on
    macOS, ~8 KB on Linux).
  * Parking the run(1) fiber from inside a Coro body (a sleep, a channel
    recv) crashes; it must either work or raise.

A Coro's frame chain dangling after its first resumer returned, and its parked
frames being invisible to the GC, were gaps here too; tests/test_coro_frames.py
guards their fixes.
"""
import signal
import sys

import pytest

from adv_util import run_python
from known_gaps import KNOWN_GAP


def _run(code, timeout=60):
    p = run_python(code, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _lines(out, tag):
    return [l[len(tag):].strip() for l in out.splitlines() if l.startswith(tag)]


def _fail_unless_crash_is(rc, err, what):
    """A death by a signal other than SIGSEGV/SIGBUS is not the known break."""
    if rc < 0 and -rc not in (signal.SIGSEGV, signal.SIGBUS):
        pytest.fail("%s: killed by signal %d, not the known SIGSEGV/SIGBUS: %s"
                    % (what, -rc, err[-1500:]))


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
           "Coro stacks (2 per thread, each a 512 KB mapping)",
           raises=AssertionError)
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
           "out from under the Coro's stack and crashes",
           raises=AssertionError)
@pytest.mark.parametrize("mode", ["sleep", "chan_recv"])
def test_parking_the_run1_fiber_inside_a_coro_body_works_or_raises(mode):
    rc, out, err = _run(RUN1_PARK % mode, timeout=30)
    if "PARKING" not in out:
        pytest.fail("the Coro body never ran: rc=%s %s" % (rc, err[-1500:]))
    _fail_unless_crash_is(rc, err, mode)
    # Either outcome is acceptable: the park works, or it raises in the body.
    assert rc == 0 and _lines(out, "DONE"), (
        "parking inside a Coro body crashed", rc, out, err[-500:])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
