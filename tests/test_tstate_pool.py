"""A finished fiber's thread state is reset and reused by the next spawn.

Every M:N fiber runs on its own PyThreadState, and creating and destroying one
was most of a spawn's cost: a calloc of the whole state, a QSBR slot reserved
and unregistered, a TLBC index, the mimalloc heaps bound, the interpreter's
HEAD_LOCK twice and a biased-refcount bucket insert and remove -- several of
them under interpreter-wide mutexes, so spawns on different hubs serialized.
Now a finished fiber's state is reset (runloom_iframe_recycle_fiber_tstate)
and kept in its hub thread's pool, and the next spawn there takes it.

The reset clears what PyThreadState_Clear clears for the objects a state
holds: what one fiber left in thread-locals, its context, its exception, the
asyncio running loop and the async-generator hooks must not reach the next.
A state the reset does not cover (left tracing, say) is cleared as before.
STACKWEAVE_TSTATE_POOL sizes each hub's pool (0 turns it off);
stats()["tstate_pool_hits"] counts the spawns it served once a run ends.
"""
import sys

import pytest

from adv_util import run_python


def _assert_pass(code, timeout=60, env=None):
    # the default pool size unless a test sets one
    p = run_python(code, timeout=timeout,
                   env=env or {"STACKWEAVE_TSTATE_POOL": None})
    assert p.returncode == 0 and "PASS" in p.stdout, (
        p.returncode, p.stdout[-2000:], p.stderr[-4000:])
    return p.stdout


def test_reused_states_carry_nothing_from_the_last_fiber():
    _assert_pass(r"""
import asyncio, contextvars, sys, threading
import stackweave, stackweave_c
loc = threading.local()
cv = contextvars.ContextVar("cv", default="unset")
def hook(agen):
    pass
bad = []
N = 4000
done = bytearray(N)
def fiber(i):
    if getattr(loc, "v", None) is not None:
        bad.append(("threading.local", i, loc.v))
    if cv.get() != "unset":
        bad.append(("contextvar", i, cv.get()))
    if sys.exception() is not None:
        bad.append(("exception", i, sys.exception()))
    if asyncio.events._get_running_loop() is not None:
        bad.append(("running loop", i))
    if sys.get_asyncgen_hooks().firstiter is not None:
        bad.append(("asyncgen hooks", i))
    # leave all of it behind for whichever fiber gets this state next
    loc.v = i
    cv.set(i)
    asyncio.events._set_running_loop(object())
    sys.set_asyncgen_hooks(firstiter=hook)
    try:
        raise KeyError(i)
    except KeyError:
        done[i] = 1
def main():
    for i in range(N):
        stackweave.fiber(lambda i=i: fiber(i))
    while sum(done) < N:
        stackweave.sleep(0.001)
stackweave.run(4, main)
hits = stackweave_c.stats()["tstate_pool_hits"]
print("hits", hits, "bad", bad[:3])
assert not bad, bad[:5]
assert hits > N // 4, hits
print("PASS")
""")


def test_a_state_left_tracing_is_not_reused():
    # sys.settrace inside a fiber traces that state; a fiber that ends still
    # tracing is cleared, not pooled, so no later fiber runs under its tracer.
    _assert_pass(r"""
import sys
import stackweave, stackweave_c
calls = []
def tracer(frame, event, arg):
    calls.append(event)
    return None
def leaves_trace_on():
    sys.settrace(tracer)
def plain():
    def f():
        return 1
    for _ in range(50):
        f()
def main():
    for _ in range(200):
        stackweave.fiber(leaves_trace_on)
    stackweave.sleep(0.2)
    n = len(calls)
    for _ in range(2000):
        stackweave.fiber(plain)
    stackweave.sleep(0.5)
    assert len(calls) == n, (n, len(calls))
stackweave.run(2, main)
print("PASS")
""")


def test_the_pool_can_be_turned_off():
    _assert_pass(r"""
import stackweave, stackweave_c
def main():
    for _ in range(2000):
        stackweave.fiber(lambda: None)
    stackweave.sleep(0.2)
stackweave.run(4, main)
assert stackweave_c.stats()["tstate_pool_hits"] == 0, stackweave_c.stats()["tstate_pool_hits"]
print("PASS")
""", env={"STACKWEAVE_TSTATE_POOL": "0"})


def test_pooled_states_survive_a_fork_and_another_run():
    # Pooled states sit outside every biased-refcount bucket; a forked child's
    # CPython clears them with every other thread's state, and a second run in
    # the parent starts from drained pools.
    _assert_pass(r"""
import os, warnings
import stackweave, stackweave_c
warnings.simplefilter("ignore", DeprecationWarning)   # fork() while threaded
out = {}
def main():
    for _ in range(3000):
        stackweave.fiber(lambda: None)
    stackweave.sleep(0.3)
    pid = os.fork()
    if pid == 0:
        # CPython's after-fork code has cleared every other thread state,
        # pooled ones included, by the time fork() returns here
        import gc
        gc.collect()
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    out["status"] = status
stackweave.run(4, main)
assert out["status"] == 0, out
hits1 = stackweave_c.stats()["tstate_pool_hits"]
stackweave.run(4, lambda: [stackweave.fiber(lambda: None) for _ in range(3000)])
hits2 = stackweave_c.stats()["tstate_pool_hits"]
print("hits", hits1, hits2)
assert hits1 > 0 and hits2 > hits1, (hits1, hits2)
print("PASS")
""", timeout=90)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
