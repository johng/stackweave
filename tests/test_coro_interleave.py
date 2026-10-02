"""Coros taking turns on one thread each keep their own interpreter state.

Coro.resume swapped only the recursion counter and current_frame, so Coros
taking turns shared the rest of the thread state:
  * the critical-section chain: a Coro parked inside a critical section (a
    list(map()) callback holds the new list's, list.extend() the extended
    list's) left its node at the head of the chain, still holding the mutex,
    and the next Coro to push or pop linked through the other's stack -- two
    Coros one level deep SIGSEGVed;
  * the datastack: the first Coro to return popped the frame top back below
    frames another still owned, and the next push overwrote them -- eight
    Coros recursing 50 Python levels deep SIGSEGVed.
Each Coro now swaps both (and its c_stack_refs list), as the scheduler does for
a fiber.  Each case runs in a subprocess, since a regression is a crash.
"""
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run(code):
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       env=dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src"),
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


TAKE_TURNS = """
import sys, stackweave_c
def take_turns(coros):
    while not all(c.done for c in coros):
        for c in coros:
            if not c.done:
                c.resume()
"""


@pytest.mark.parametrize("n, depth", [(2, 1), (3, 50), (8, 40)])
def test_coros_parked_inside_critical_sections_take_turns(n, depth):
    # list(map()) holds a critical section on the list it builds across each callback,
    # and each level parks inside one.
    rc, out, err = _run(TAKE_TURNS + """
N, DEPTH = %d, %d
def crec(n):
    stackweave_c.yield_()
    if n == DEPTH:
        return n
    return list(map(lambda k: crec(k), [n + 1]))[0]
got = []
coros = [stackweave_c.Coro(lambda: got.append(crec(0))) for _ in range(N)]
take_turns(coros)
print("RESULTS", got)
""" % (n, depth))
    assert rc == 0, (rc, err[-2000:])
    assert "RESULTS %r" % ([depth] * n,) in out, out


@pytest.mark.parametrize("n, depth", [(3, 50), (8, 50), (16, 100)])
def test_coros_recursing_in_python_take_turns(n, depth):
    # Plain Python recursion: frames only on the datastack.
    rc, out, err = _run(TAKE_TURNS + """
N, DEPTH = %d, %d
def r(n, i):
    stackweave_c.yield_()
    return i if n == DEPTH else r(n + 1, i)
got = []
coros = [stackweave_c.Coro(lambda i=i: got.append(r(0, i))) for i in range(N)]
take_turns(coros)
print("RESULTS", sorted(got))
""" % (n, depth))
    assert rc == 0, (rc, err[-2000:])
    assert "RESULTS %r" % (list(range(n)),) in out, out


def test_a_parked_coro_does_not_hold_its_critical_section():
    # shared.extend(map(...)) holds `shared`'s critical section while it pulls
    # each item, and the Coro parks inside that.  Parked, it must have released
    # it: another OS thread appends to `shared` while this one busy-spins (a
    # blocking wait would detach this thread, which releases the section anyway
    # and hides the bug).  Resumed, the extend finishes.
    rc, out, err = _run(TAKE_TURNS + """
import threading, time
shared = [0, 1, 2]
c = stackweave_c.Coro(lambda: shared.extend(map(lambda x: (stackweave_c.yield_(), x)[1], [7, 8])))
c.resume()                       # parked inside shared.extend
done = []
t = threading.Thread(target=lambda: (shared.append(99), done.append(1)))
t.start()
end = time.monotonic() + 3
while not done and time.monotonic() < end:
    pass
print("APPENDED_WHILE_PARKED", bool(done))
while not c.done:
    c.resume()
t.join()
print("SHARED", sorted(shared))
""")
    assert rc == 0, (rc, err[-2000:])
    assert "APPENDED_WHILE_PARKED True" in out, out
    assert "SHARED [0, 1, 2, 7, 8, 99]" in out, out


def test_coros_dropped_while_parked_leave_the_thread_usable():
    # Parked Coros dropped mid-recursion give back their own datastacks; the
    # thread then runs more Coros and Python as usual.
    rc, out, err = _run(TAKE_TURNS + """
def r(n):
    stackweave_c.yield_()
    if n:
        r(n - 1)
for _ in range(200):
    cs = [stackweave_c.Coro(lambda: r(30)) for _ in range(4)]
    for c in cs:
        for _ in range(10):
            c.resume()
    del cs, c
coros = [stackweave_c.Coro(lambda: r(30)) for _ in range(4)]
take_turns(coros)
print("OK", sum(range(1000)))
""")
    assert rc == 0, (rc, err[-2000:])
    assert "OK 499500" in out, out
