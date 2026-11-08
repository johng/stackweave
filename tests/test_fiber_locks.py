"""A stock threading.Lock / RLock wait parks the fiber, not its hub's OS thread.

_thread.lock and _thread.RLock wait in CPython's parking lot, which sleeps the
OS thread.  Under M:N that thread is a hub: a fiber waiting for a lock a parked
fiber holds took its hub with it, and with as many waiters as hubs nothing
could run the holder -- a hard hang the deadlock detector cannot see (the hubs
are busy, not idle).  The exec-home patch's parking_lot.c hunk lets the lock
objects' waits ask the fiber runtime first (stackweave_c.fiber_lock_hooks;
src/runloom_c/runloom_fiberlock.c installs the hooks at import), and a fiber
parks through the same in-memory park every cooperative primitive uses.  The
parking lot keeps its queue, handoff and timeout bookkeeping.

Every test here uses the STOCK lock classes, never monkey.patch(), and checks
stats()["fiber_lock_parks"] so a run on an interpreter that silently fell
back to blocking the thread fails rather than passes by luck.
"""
import os
import sys

import pytest

from adv_util import run_python

HERE = os.path.dirname(os.path.abspath(__file__))

PRELUDE = r'''
import os, sys, threading, time
import stackweave, stackweave_c

assert stackweave_c.fiber_lock_hooks, "interpreter built without the exec-home parking_lot.c hunk"
assert stackweave_c.fiber_locks_installed, "fiber lock hooks not installed"

def _watchdog(secs):
    def fire():
        print("WATCHDOG TIMEOUT after %ss" % secs, flush=True)
        import faulthandler; faulthandler.dump_traceback(all_threads=True)
        os._exit(3)
    t = threading.Timer(secs, fire); t.daemon = True; t.start()

def foreign_send(ch, value):
    while True:
        try:
            ch.send(value)
            return
        except RuntimeError:
            time.sleep(0.0005)

def foreign_wake(ch, i):
    """Park on `ch` until a plain OS thread sends: the wake goes through the
    global run-queue, so the fiber may resume on another hub."""
    t = threading.Thread(target=lambda: (time.sleep(0.001), foreign_send(ch, i)), daemon=True)
    t.start(); ch.recv(); t.join()

def parks():
    return stackweave_c.stats()["fiber_lock_parks"]
'''


def assert_pass(code, timeout=60, env=None):
    p = run_python(PRELUDE + code, timeout=timeout, env=env)
    assert p.returncode == 0, "rc=%s\n--- stdout ---\n%s\n--- stderr ---\n%s" % (
        p.returncode, p.stdout[-4000:], p.stderr[-4000:])
    assert "PASS" in p.stdout, p.stdout[-2000:]


@pytest.mark.parametrize("lock", ["threading.Lock", "threading.RLock"])
def test_more_waiters_than_hubs_on_a_lock_held_across_parks(lock):
    """The hang: six fibers on four hubs, each holding the lock across twenty
    foreign-woken parks.  Blocking the hub's thread, four waiters stopped
    every hub and the holder never ran again."""
    assert_pass(r'''
_watchdog(40)
LOCK = %s
def main():
    lk = LOCK()
    res = {"n": 0, "moved": 0}
    def worker(i):
        ch = stackweave.Chan(0)
        h0 = stackweave_c.mn_current_hub()
        with lk:
            for k in range(20):
                foreign_wake(ch, k)
            res["moved"] += stackweave_c.mn_current_hub() != h0
        res["n"] += 1
    for i in range(6):
        stackweave.fiber(worker, i)
    t0 = time.monotonic()
    while res["n"] < 6:
        stackweave.sleep(0.01)
    print("done in %%.2fs, %%d migrated holding the lock, parks=%%d" %% (time.monotonic() - t0, res["moved"], parks()), flush=True)
    assert parks() >= 5, "no waiter parked: %%d" %% parks()
    assert lk.acquire(timeout=1); lk.release()
    print("PASS", flush=True)
stackweave.run(4, main)
''' % lock)


def test_a_same_hub_waiter_lets_the_holder_run():
    """Holder and waiter pinned to one hub, the holder asleep inside the lock.
    Its timer is popped only by that hub, so a waiter that blocked the hub's
    thread could only ever time out; a waiter that parks gets the lock when
    the holder wakes and releases."""
    assert_pass(r'''
_watchdog(30)
def main():
    lk = threading.RLock()
    log = []
    done = stackweave.Chan(0)
    def holder():
        stackweave_c.current_g().pin(0); stackweave.sleep(0)
        with lk:
            log.append(("in", time.monotonic()))
            stackweave.sleep(0.2)
            log.append(("out", time.monotonic()))
        done.send(1)
    def waiter():
        stackweave_c.current_g().pin(0); stackweave.sleep(0.05)
        t0 = time.monotonic()
        with lk:                                   # no timeout
            log.append(("waiter in", time.monotonic(), stackweave_c.mn_current_hub()))
        done.send(2)
    stackweave.fiber(holder); stackweave.fiber(waiter)
    done.recv(); done.recv()
    names = [e[0] for e in log]
    assert names == ["in", "out", "waiter in"], names
    waited = log[2][1] - log[0][1]
    assert 0.15 <= waited < 2.0, waited
    assert log[2][2] == 0
    assert parks() >= 1, parks()
    print("PASS", flush=True)
stackweave.run(4, main)
''')


def test_a_timed_acquire_on_a_held_lock_times_out_then_succeeds():
    """acquire(timeout=) on a lock a parked fiber holds returns False on time,
    and the waiter's later acquire succeeds once the holder has released: the
    parking lot's timeout-vs-unpark race is still its own."""
    assert_pass(r'''
_watchdog(30)
def main():
    lk = threading.Lock()
    res = {}
    done = stackweave.Chan(0)
    def holder():
        with lk:
            stackweave.sleep(0.5)
        done.send(1)
    def waiter():
        stackweave.sleep(0.05)
        t0 = time.monotonic()
        res["first"] = lk.acquire(timeout=0.15)
        res["first_took"] = time.monotonic() - t0
        t0 = time.monotonic()
        res["second"] = lk.acquire(timeout=5)
        res["second_took"] = time.monotonic() - t0
        if res["second"]:
            lk.release()
        done.send(2)
    stackweave.fiber(holder); stackweave.fiber(waiter)
    done.recv(); done.recv()
    print(res, flush=True)
    assert res["first"] is False and 0.1 <= res["first_took"] < 1.0, res
    assert res["second"] is True and 0.2 <= res["second_took"] < 2.0, res
    assert parks() >= 2, parks()
    print("PASS", flush=True)
stackweave.run(2, main)
''')


def test_a_plain_thread_holder_and_a_fiber_waiter():
    """A plain OS thread holds the lock; a fiber waits.  The wait parks the
    fiber (a sibling pinned to the same hub keeps running meanwhile), and the
    thread's release wakes it from a foreign thread."""
    assert_pass(r'''
_watchdog(30)
def main():
    lk = threading.Lock()
    lk.acquire()                                   # main is a fiber: hand over to a thread
    held = threading.Event()
    def thread():
        held.set()
        time.sleep(0.3)
        lk.release()
    ticks = {"n": 0}
    stop = {"v": False}
    def sibling():
        stackweave_c.current_g().pin(0)
        while not stop["v"]:
            ticks["n"] += 1
            stackweave.sleep(0.005)
    def waiter():
        stackweave_c.current_g().pin(0); stackweave.sleep(0)
        held.wait()
        t0 = time.monotonic()
        lk.acquire()
        res["took"] = time.monotonic() - t0
        res["ticks_meanwhile"] = ticks["n"]
        lk.release()
        stop["v"] = True
    res = {}
    threading.Thread(target=thread, daemon=True).start()
    stackweave.fiber(sibling)
    w = stackweave.fiber(waiter)
    while not stop["v"]:
        stackweave.sleep(0.01)
    print(res, flush=True)
    assert 0.2 <= res["took"] < 2.0, res
    assert res["ticks_meanwhile"] >= 10, "the sibling on the waiter's hub starved: %r" % res
    assert parks() >= 1, parks()
    print("PASS", flush=True)
stackweave.run(2, main)
''')


def test_a_fiber_holder_and_a_plain_thread_waiter():
    """A plain thread waiting for a lock a fiber holds blocks on the platform
    semaphore as always (it has no fiber to park) and gets the lock on
    release, including after the holder migrated."""
    assert_pass(r'''
_watchdog(30)
def main():
    lk = threading.RLock()
    res = {}
    ch = stackweave.Chan(0)
    with lk:
        def thread():
            t0 = time.monotonic()
            with lk:
                res["took"] = time.monotonic() - t0
        t = threading.Thread(target=thread); t.start()
        time.sleep(0.05)
        for k in range(10):
            foreign_wake(ch, k)                    # may migrate while holding
        stackweave.sleep(0.2)
    t.join(5)
    assert not t.is_alive(), "the thread never got the lock"
    assert 0.2 <= res["took"] < 2.0, res
    print("PASS", flush=True)
stackweave.run(4, main)
''')


def test_condition_on_a_stock_rlock_across_hubs():
    """threading.Condition over a stock RLock: wait() releases through
    _release_save, parks on the waiter lock, and re-takes the RLock through
    _acquire_restore -- the three waits the lock objects flag for fibers."""
    assert_pass(r'''
_watchdog(30)
def main():
    cv = threading.Condition(threading.RLock())
    items = []
    got = []
    done = stackweave.Chan(0)
    def consumer(i):
        with cv:
            while not items:
                cv.wait()
            got.append((i, items.pop()))
        done.send(i)
    def producer():
        for i in range(8):
            stackweave.sleep(0.01)
            with cv:
                items.append(i)
                cv.notify()
    for i in range(8):
        stackweave.fiber(consumer, i)
    stackweave.fiber(producer)
    for _ in range(8):
        done.recv()
    assert sorted(v for _, v in got) == list(range(8)), got
    assert parks() >= 1, parks()
    print("PASS", flush=True)
stackweave.run(4, main)
''')


def test_single_thread_run1_fibers_park_too():
    """run(1) has no hubs: a fiber of the single-thread scheduler parks on its
    own scheduler, and the holder's timer fires in the same drain."""
    assert_pass(r'''
_watchdog(30)
def main():
    lk = threading.Lock()
    log = []
    def holder():
        with lk:
            log.append("in"); stackweave.sleep(0.1); log.append("out")
    def waiter():
        stackweave.sleep(0.02)
        with lk:
            log.append("waiter")
    stackweave.fiber(holder); stackweave.fiber(waiter)
    stackweave.sleep(0.5)
    assert log == ["in", "out", "waiter"], log
    assert parks() >= 1, parks()
    print("PASS", flush=True)
stackweave.run(1, main)
''')


@pytest.mark.parametrize("hubs", [1, 2], ids=["run1", "mn"])
def test_a_wait_inside_a_subinterpreter_blocks_the_thread(hubs):
    """A fiber that calls into a subinterpreter (_interpreters.exec) runs that
    code with the subinterpreter's thread state attached, and its lock waits
    reach the same process-wide hooks.  Parking there handed the run(1) drain
    or the hub a foreign state (run(1): SIGSEGV), so such a wait blocks the
    thread as before the hooks; the fiber's other waits still park."""
    assert_pass(r'''
import _interpreters
_watchdog(90)
CODE = """
import threading, time
l = threading.Lock(); l.acquire()
t = threading.Thread(target=lambda: (time.sleep(0.2), l.release()))
t.start()
l.acquire(); l.release()
t.join()
"""
def main():
    iid = _interpreters.create()
    ticks = []
    def sibling():
        for i in range(20):
            stackweave.sleep(0.01); ticks.append(i)
    stackweave.fiber(sibling)
    t0 = time.monotonic()
    assert _interpreters.exec(iid, CODE) is None
    took = time.monotonic() - t0
    _interpreters.destroy(iid)
    assert 0.15 <= took < 5.0, took
    assert parks() == 0, "a wait in the subinterpreter parked the fiber: %d" % parks()
    deadline = time.monotonic() + 30       # a slow runner: wait for it, not a fixed time
    while len(ticks) < 20 and time.monotonic() < deadline:
        stackweave.sleep(0.01)
    assert len(ticks) == 20, "the sibling stalled after the wait: %r" % (ticks,)
    lk = threading.Lock()                  # and the fiber's own waits still park
    def holder():
        with lk:
            stackweave.sleep(0.1)
    stackweave.fiber(holder)
    deadline = time.monotonic() + 30
    while not lk.locked() and time.monotonic() < deadline:
        stackweave.sleep(0.005)
    with lk:                               # contended: the holder has it
        pass
    assert parks() >= 1, parks()
    print("PASS", flush=True)
stackweave.run(HUBS, main)
'''.replace("HUBS", str(hubs)))


def test_an_aborted_mn_run_drops_its_lock_waiters():
    """An M:N run() that ends abnormally (here a signal carried out of it)
    leaves a fiber parked in a lock wait and frees the hubs.  mn_fini's
    teardown dequeues its parking-lot entry and drops wakes for it: a later
    release must not queue it on a freed hub (use-after-free), hand it the
    lock (wedged for good), or leave its run-alive count behind (a later
    run(1) would never return)."""
    assert_pass(r'''
import signal
_watchdog(40)
lk = threading.Lock()
held = threading.Event(); release = threading.Event()
def holder():
    with lk:
        held.set()
        release.wait()
t = threading.Thread(target=holder); t.start(); held.wait()
def main():
    def waiter():
        lk.acquire()                       # parks: a plain thread holds it
        lk.release()
    stackweave.fiber(waiter)
    deadline = time.monotonic() + 10
    while parks() < 1 and time.monotonic() < deadline:
        stackweave.sleep(0.01)
    os.kill(os.getpid(), signal.SIGINT)    # carried out of mn_run
    stackweave.sleep(10)
try:
    stackweave.run(2, main)
    raise SystemExit("run() returned normally")
except KeyboardInterrupt:
    pass
st = stackweave_c.stats()
assert st["fiber_lock_parks"] >= 1, st["fiber_lock_parks"]
assert st["fiber_lock_stale_wakes"] >= 1, "the teardown did not drop the waiter"
assert st["foreign_park_inflight"] == 0, st["foreign_park_inflight"]
release.set(); t.join()                    # the release finds no waiter
assert lk.acquire(timeout=2), "the lock was handed to a fiber that never runs"
lk.release()
stackweave.run(1, lambda: None)            # returns: no run-alive count leaked
def again():
    with lk:
        pass
stackweave.run(2, again)
print("PASS", flush=True)
''')


def test_a_lock_wait_inside_a_coro_blocks_the_thread():
    """A run(1) fiber that resumes a _Coro whose body waits for a stock lock:
    parking would mark the OUTER fiber parked while only the Coro yields (the
    Coro's resume returns early, and the wake resumes the outer fiber twice).
    The wait blocks the thread instead, as before the hooks."""
    assert_pass(r'''
_watchdog(30)
def main():
    lk = threading.Lock(); lk.acquire()
    threading.Timer(0.2, lk.release).start()
    log = []
    def body():
        with lk:
            log.append("coro got it")
        return "done"
    c = stackweave_c._Coro(body)
    before = parks()                       # (Timer.start's own handshake may park)
    t0 = time.monotonic()
    c.resume()
    assert c.done and c.result == "done", (c.done, log)
    assert log == ["coro got it"] and time.monotonic() - t0 >= 0.1, log
    assert parks() == before, "the Coro's wait parked the fiber: %d" % (parks() - before)
    stackweave.sleep(0.05)                 # the outer fiber is intact
    print("PASS", flush=True)
stackweave.run(1, main)
''')


def test_a_lock_wait_inside_object_destruction_blocks_the_thread():
    """A finalizer that waits for a contended stock lock while CPython is
    destroying objects (a trashcan unwind: delete_later holds more) must not
    park: a parked fiber would freeze a half-finished destructor across a
    GC-safe point and could resume it on another hub -- the gate the
    preemption yield sites honour."""
    assert_pass(r'''
_watchdog(40)
lk = threading.Lock()
state = {"seen": 0, "done": False}
class Del:
    def __del__(self):
        if state["done"] or not stackweave_c._tstate_in_destruction():
            return
        state["done"] = True
        state["seen"] = 1
        before = parks()
        threading.Timer(0.1, lk.release).start()
        lk.acquire()                        # contended: held until the timer
        lk.release()
        state["parked"] = parks() - before
def main():
    def build():
        root = cur = []
        for _ in range(200000):
            nxt = [Del(), None, Del()]
            cur.append(nxt)
            cur = nxt
        return root
    root = build()
    lk.acquire()
    del root                               # deep dealloc: the trashcan defers
    print(state, flush=True)
    assert state["seen"], "no finalizer ran inside a trashcan unwind"
    assert state["parked"] == 0, "a wait inside destruction parked the fiber"
    print("PASS", flush=True)
stackweave.run(2, lambda: stackweave.fiber(main) and None)
''', timeout=90)


def test_a_wake_that_finds_the_waiter_running_leaves_no_credit():
    """A stray wake (g.wake()) resumes a fiber parked in a lock wait; the
    release's wake then finds it queued, not parked, and leaves a credit.  The
    wait takes it back: the fiber's next, unrelated park must still wait."""
    assert_pass(r'''
_watchdog(30)
def main():
    lk = threading.Lock()
    res = {}
    ready = stackweave.Chan(1)
    done = stackweave.Chan(1)
    def waiter():
        stackweave_c.current_g().pin(0); stackweave.sleep(0)
        ready.send(stackweave_c.current_g())
        with lk:                           # parks: the holder has it
            pass
        t0 = time.monotonic()
        res["timed_out"] = stackweave_c.park(timeout=0.3)
        res["took"] = time.monotonic() - t0
        done.send(1)
    def holder():
        stackweave_c.current_g().pin(0); stackweave.sleep(0)
        lk.acquire()
        stackweave.fiber(waiter)
        wg, _ = ready.recv()
        deadline = time.monotonic() + 10
        while parks() < 1 and time.monotonic() < deadline:
            stackweave.sleep(0.01)
        wg.wake()                          # resumes it: nothing to do with the lock
        lk.release()                       # its wake finds the waiter queued
    stackweave.fiber(holder)
    done.recv()
    print(res, flush=True)
    assert res["timed_out"] is True and res["took"] >= 0.25, (
        "a stale wake credit ended the next park at once: %r" % (res,))
    print("PASS", flush=True)
stackweave.run(2, main)
''')


def test_a_timed_wait_arms_one_timer_however_often_it_wakes():
    """A timed lock wait that is woken spuriously re-parks on the timer entry
    it already armed: one per wait on its hub, not one per park.  Each extra
    entry would fire a spurious wake into the fiber's later parks."""
    assert_pass(r'''
_watchdog(30)
def main():
    lk = threading.Lock()
    res = {}
    ready = stackweave.Chan(1)
    done = stackweave.Chan(1)
    def waiter():
        stackweave_c.current_g().pin(0); stackweave.sleep(0)
        ready.send(stackweave_c.current_g())
        res["arms0"] = stackweave_c.stats()["fiber_lock_timer_arms"]
        res["got"] = lk.acquire(timeout=5)
        res["arms"] = stackweave_c.stats()["fiber_lock_timer_arms"] - res["arms0"]
        lk.release()
        done.send(1)
    def holder():
        stackweave_c.current_g().pin(0); stackweave.sleep(0)
        lk.acquire()
        stackweave.fiber(waiter)
        wg, _ = ready.recv()
        deadline = time.monotonic() + 10
        while parks() < 1 and time.monotonic() < deadline:
            stackweave.sleep(0.01)
        for _ in range(5):
            wg.wake()                      # spurious: it re-parks
            stackweave.sleep(0.01)
        lk.release()
    stackweave.fiber(holder)
    done.recv()
    print(res, "parks", parks(), flush=True)
    assert res["got"] is True, res
    assert res["arms"] == 1, "timer entries armed for one wait: %r" % (res,)
    assert parks() >= 6, parks()
    print("PASS", flush=True)
stackweave.run(2, main)
''')


def test_a_signal_interrupts_a_run1_fiber_lock_wait_on_the_main_thread():
    """Ctrl-C interrupts a stock thread's lock.acquire() on the main thread.  A
    run(1) fiber's wait parks instead, so the drain wakes it with a mark; the
    wait returns Py_PARK_INTR and CPython runs the handler in the fiber, which
    raises out of acquire() there.  A handler that does not raise lets the
    wait go on."""
    assert_pass(r'''
import signal
_watchdog(30)
lk = threading.Lock()
def hold(secs):
    def run():
        with lk:
            time.sleep(secs)
    t = threading.Thread(target=run, daemon=True); t.start()
    while not lk.locked():
        time.sleep(0.001)
    return t
out = {}
def main():
    t = hold(3.0)
    threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
    t0 = time.monotonic()
    try:
        lk.acquire()
        out["first"] = "acquired"
        lk.release()
    except KeyboardInterrupt:
        out["first"] = "interrupted after %.1f" % (time.monotonic() - t0)
    t.join()
    hits = []
    old = signal.signal(signal.SIGALRM, lambda *a: hits.append(1))
    try:
        t = hold(0.5)
        signal.setitimer(signal.ITIMER_REAL, 0.1)
        assert lk.acquire(timeout=5)       # the handler runs, the wait goes on
        lk.release()
        out["second"] = hits
    finally:
        signal.signal(signal.SIGALRM, old)
    t.join()
stackweave.run(1, main)
print(out, stackweave_c.stats()["fiber_lock_signal_wakes"], flush=True)
assert out["first"].startswith("interrupted after 0.") , out
assert out["second"] == [1], out
assert stackweave_c.stats()["fiber_lock_signal_wakes"] >= 2
print("PASS", flush=True)
''')


def test_a_plain_thread_rlock_across_pygilstate_callbacks_is_stock():
    """Only a FIBER's thread state owns its locks; every other state keeps the
    OS thread, as stock.  A C library's thread that calls back into Python
    once per event gets a fresh PyGILState thread state each time, and may take
    an RLock in one callback and release it in the next."""
    assert_pass(r'''
lk = threading.RLock()
out = stackweave_c._c_thread_call_each([
    lambda: lk.acquire(),
    lambda: lk._is_owned(),
    lambda: lk.release(),
    lambda: lk._is_owned(),
])
print(out, flush=True)
assert out == [True, True, None, False], out
assert stackweave_c.rlock_tstate_owned
print("PASS", flush=True)
''')


def test_a_forked_child_keeps_its_threads_lock_waiters():
    """CPython empties the parking lot in a forked child.  The forking
    thread's run(1) fibers live on, so one parked in a lock wait must retry
    with a fresh entry: the child's own release then wakes it.  Every other
    waiter is dead, and only its run-alive count is given back."""
    assert_pass(r'''
import warnings
warnings.simplefilter("ignore", DeprecationWarning)   # fork with threads
_watchdog(30)
lk = threading.Lock()
log = []
def main():
    def waiter():
        with lk:
            log.append("waiter")
    lk.acquire()
    stackweave.fiber(waiter)
    deadline = time.monotonic() + 10
    while parks() < 1 and time.monotonic() < deadline:
        stackweave.sleep(0.01)
    pid = os.fork()
    if pid == 0:
        lk.release()                       # the waiter retried: this wakes it
        stackweave.sleep(0.2)
        ok = log == ["waiter"]
        os.write(1, ("child log %r\n" % (log,)).encode())
        os._exit(0 if ok else 1)
    lk.release()
    _, st = os.waitpid(pid, 0)
    out["child"] = os.waitstatus_to_exitcode(st)
out = {}
stackweave.run(1, main)
assert out.get("child") == 0, "the child's waiter never ran: %r" % (out,)
assert log == ["waiter"], log
assert stackweave_c.stats()["foreign_park_inflight"] == 0
print("PASS", flush=True)
''')


def test_a_woken_lock_waiter_dropped_by_sched_reset():
    """A run(1) fiber woken from a lock wait but dropped by sched_reset before
    it ran (a stackweave.aio loop stopped with fibers parked, then closed):
    freeing it unlinks it from its sched's waiter list (it lives in the g, not
    on the freed stack) and gives back its run-alive count, so the next run(1)
    neither walks a freed node nor waits for a park that never ends."""
    assert_pass(r'''
_watchdog(40)
lk = threading.Lock()
held = threading.Event(); release = threading.Event()
def holder():                              # a plain thread holds the lock
    with lk:
        held.set()
        release.wait()
t = threading.Thread(target=holder); t.start(); held.wait()
def main():
    def waiter():
        with lk:
            pass
    stackweave.fiber(waiter)
    deadline = time.monotonic() + 10
    while parks() < 1 and time.monotonic() < deadline:
        stackweave.sleep(0.01)
    stackweave_c.sched_stop()              # the drain returns, the waiter parked
stackweave.run(1, main)
release.set(); t.join()                    # wakes it: queued, never run
time.sleep(0.05)
print("reset", stackweave_c.sched_reset(), flush=True)
assert stackweave_c.stats()["foreign_park_inflight"] == 0, stackweave_c.stats()["foreign_park_inflight"]
# (lk itself may stay locked for good: its release could hand it to the
# dropped fiber, which never runs -- a documented limitation.)
lk2 = threading.Lock()
def again():
    def w():
        with lk2:
            pass
    lk2.acquire()
    for _ in range(3):
        stackweave.fiber(w)
    stackweave.sleep(0.05)
    lk2.release()
stackweave.run(1, again)                   # returns, and walks its list
assert stackweave_c.stats()["foreign_park_inflight"] == 0
print("PASS", flush=True)
''')


def test_a_timed_wait_that_ends_early_leaves_no_live_timer():
    """A timed acquire that gets the lock early must not leave its timer entry
    live: it would wake the fiber's next, unrelated park at the old deadline,
    pin the g until then, and a loop of acquire(timeout=3600) would grow the
    timer heap without bound.  The wait bumps its timer generation; the heap
    drops and purges stale entries."""
    assert_pass(r'''
_watchdog(60)
def main():
    lk = threading.Lock()
    res = {}
    done = stackweave.Chan(1)
    ping = stackweave.Chan(1)
    pong = stackweave.Chan(1)
    N = 200
    def waiter():
        stackweave_c.current_g().pin(0); stackweave.sleep(0)
        assert lk.acquire(timeout=0.3)      # granted at ~0.05
        lk.release()
        t0 = time.monotonic()
        res["timed_out"] = stackweave_c.park(timeout=0.6)
        res["took"] = time.monotonic() - t0
        arms0 = stackweave_c.stats()["fiber_lock_timer_arms"]
        for _ in range(N):                  # each a contended timed wait
            ping.recv()
            assert lk.acquire(timeout=3600)
            lk.release()
            pong.send(1)
        stackweave.sleep(0.01)
        res["arms"] = stackweave_c.stats()["fiber_lock_timer_arms"] - arms0
        res["entries"] = stackweave_c.stats()["timer_entries"]
        done.send(1)
    def holder():
        stackweave_c.current_g().pin(0); stackweave.sleep(0)
        lk.acquire()
        stackweave.fiber(waiter)
        stackweave.sleep(0.05)
        lk.release()
        stackweave.sleep(0.7)               # past the waiter's park(timeout=0.6)
        for _ in range(N):
            lk.acquire()
            ping.send(1)
            stackweave.sleep(0.001)         # the waiter parks on the lock
            lk.release()
            pong.recv()
    stackweave.fiber(holder)
    done.recv()
    print(res, flush=True)
    assert res["timed_out"] is True and res["took"] >= 0.5, (
        "the old wait's timer woke the next park: %r" % (res,))
    assert res["arms"] >= 150, "too few timed waits to measure: %r" % (res,)
    assert res["entries"] < 70, "stale timer entries piled up: %r" % (res,)
    print("PASS", flush=True)
stackweave.run(2, main)
''')
