"""What must hold for a fiber after it migrates to another hub.

A migrated fiber must free what it drops, keep the scheduler's timers and
queues intact, still be the same fiber to code keyed on the OS thread, and
not cost much more than a fiber that never moved.  Each test here states
one such invariant; the failing ones are the known gaps of migration mode,
left red on purpose until each is closed.

Under RUNLOOM_MIGRATION=1 (set for every subprocess here) a woken fiber
carries its own PyThreadState and may resume on any hub.  On an interpreter
without both migration patches the runtime gates the mode off and runs the
per-hub scheduler, and every scenario then skips as NOMIG (below) rather
than passing.  The rest of the suite rarely exercises migration --
``runloom.sleep()`` always resumes on the owning hub, and a wake performed
by another fiber lands on the WAKER's own deque (Go-style local wake), so a
channel hand-off between fibers stays on one hub -- so these tests FORCE a
migration.  A plain OS thread sends on the channel: a foreign waker has no
deque, so the fiber goes through the global run-queue and whichever hub is
idle pulls it.  The thread id is checked before and after each park, and a
scenario that never sees a migration skips loudly rather than passing.

Each scenario runs in a fresh subprocess so a lost fiber or a wedged hub is a
clean timeout rather than a hung pytest, and so one scenario's leak cannot
leak into the next.

Tests that fail today are NOT marked xfail: each one's docstring names the
finding it tracks (the migration review on johng/stackweave#23), and the
suite stays red until the gap is closed.

Names are ``test_<area>_<invariant>``.  The area is the subsystem a fix
lands in, so ``-k memory`` (or identity, sched, preempt, cost, harness)
selects one gap family:

    harness   the file's own premise (migration is observable)
    memory    the brc merge queue: cross-hub last decrefs that never run
    preempt   what the deleted sysmon wall-clock preemption guaranteed
    sched     races and leaks that came in with the global run-queue
    identity  code keyed on the OS thread after a fiber has moved
    cost      the price of one PyThreadState per fiber (bound in the name)

The invariant is the behaviour that must hold, not the bug, so the name
reads as a plain guard once the gap is closed.  The gaps that older
tests already cover when run with migration on (hub introspection, sysmon
classification, the stack pool, the seeded scheduler) are not duplicated
here.

Three tests pass today and are here to pin down what was verified to work:
that migration is actually observable through this harness, that
``PyGILState_Ensure`` callbacks (ctypes, sqlite UDFs, OpenSSL) run on the
fiber's own thread state after it has moved, and that signal delivery into
a migrating io-sleeper does not crash (the heap race it exercises is
confirmed by reading only).  The escape hatch for OS-thread-keyed code, a
per-fiber pin (``G.pin(N)``, johng/stackweave#24), is not in this tree.
"""
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Shared prelude for every subprocess: a watchdog so a wedge is a clean exit,
# and force_migrate(), which parks the caller on a channel until a helper
# fiber wakes it and reports whether the OS thread changed.  Prints NOMIG if
# no migration was observed after `rounds` parks; the test then skips loudly.
PRELUDE = r'''
import os, sys, threading, time
sys.path.insert(0, %r)
import runloom, runloom_c

def _watchdog(secs):
    def fire():
        print("WATCHDOG TIMEOUT after %%ss" %% secs, flush=True)
        import faulthandler; faulthandler.dump_traceback(all_threads=True)
        os._exit(3)
    t = threading.Timer(secs, fire); t.daemon = True; t.start()

def foreign_send(ch, value):
    """Send from a plain OS thread.  A non-fiber thread cannot block, so an
    unbuffered send raises RuntimeError while no receiver is parked yet (a
    slow box reaches the send before the fiber reaches its recv); retry
    until the hand-off happens."""
    while True:
        try:
            ch.send(value)
            return
        except RuntimeError:
            time.sleep(0.0005)

def force_migrate(rounds=40):
    """Park on a channel until a FOREIGN OS thread wakes us (a hub-thread waker
    would push us onto its own deque -- local wake -- and we would usually
    resume right there).  Returns True as soon as one wake resumes on a
    different OS thread."""
    ch = runloom.Chan(0)
    moved = False
    for i in range(rounds):
        before = threading.get_ident()
        def poke(i=i):
            time.sleep(0.001)
            foreign_send(ch, i)
        t = threading.Thread(target=poke, daemon=True)
        t.start()
        ch.recv()
        t.join()
        if threading.get_ident() != before:
            moved = True
            break
    return moved

def park_n_times(n):
    """Foreign-thread wakes only; returns the set of OS thread ids we ran on."""
    ch = runloom.Chan(0)
    tids = {threading.get_ident()}
    for i in range(n):
        def poke(i=i):
            time.sleep(0.001)
            foreign_send(ch, i)
        t = threading.Thread(target=poke, daemon=True)
        t.start()
        ch.recv()
        t.join()
        tids.add(threading.get_ident())
    return tids

def require_migration(moved):
    if not moved:
        print("NOMIG", flush=True)
        os._exit(0)
''' % os.path.join(REPO, "src")


def run_scenario(code, timeout=60, migration=True):
    env = dict(os.environ)
    env["PYTHON_GIL"] = "0"
    env["RUNLOOM_GIL"] = "0"
    env["RUNLOOM_MIGRATION"] = "1" if migration else "0"   # read once at the first mn_init
    try:
        p = subprocess.run(
            [sys.executable, "-c", PRELUDE + code],
            cwd=REPO, env=env, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode()
        err = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode()
        return 124, out, err + "\n[run_scenario: timed out after %ss]" % timeout
    return p.returncode, p.stdout, p.stderr


def _key_line(out, err):
    """The one line that says why the scenario failed: the last error or
    watchdog line on stderr, else the last thing the scenario printed."""
    for line in reversed(err.splitlines()):
        if re.match(r"\s*(\w+(Error|Exception|Interrupt)\b|WATCHDOG)", line):
            return line.strip()
    lines = [l for l in out.splitlines() if l.strip()]
    return lines[-1].strip() if lines else "(no output)"


def assert_pass(code, timeout=60):
    rc, out, err = run_scenario(code, timeout=timeout)
    if "NOMIG" in out:
        pytest.skip("no cross-hub migration observed on this machine; "
                    "the scenario needs >=2 hubs that actually trade fibers")
    if rc == 0 and "PASS" in out:
        return
    # The full transcript goes to stdout, which pytest shows under "Captured
    # stdout call" in the FAILURES section.  The failure message itself is
    # ONE line, so pytest's short summary lists every failing scenario on a
    # line of its own and a log tail (tests/run_isolated.py keeps 30 lines)
    # still shows the whole failing set, not the last two transcripts.
    print("--- scenario stdout ---\n%s\n--- scenario stderr ---\n%s" % (out, err))
    pytest.fail("rc=%s: %s" % (rc, _key_line(out, err)), pytrace=False)


# ---------------------------------------------------------------------------
# Harness sanity: migration is observable, and the thing we verified WORKS.
# ---------------------------------------------------------------------------

def test_harness_foreign_thread_wake_migrates_the_fiber():
    """The premise of this file: a channel wake at H=4 lands on another hub."""
    assert_pass(r'''
_watchdog(30)
def main():
    require_migration(force_migrate())
    print("PASS", flush=True)
runloom.run(4, main)
''')


def test_identity_pygilstate_callback_uses_the_fibers_own_tstate():
    """ctypes callbacks re-enter Python via PyGILState_Ensure.  After a
    migration the callback must run on the fiber's own thread state (its
    threading.local is visible inside) and the fiber must survive the
    Release.  Verified working; kept so a future seam change cannot regress
    it silently."""
    assert_pass(r'''
import ctypes, ctypes.util
_watchdog(30)
libc = ctypes.CDLL(ctypes.util.find_library("c"))
CMP = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
loc = threading.local()
seen = []
@CMP
def cmp(a, b):
    seen.append(getattr(loc, "tag", None))
    return a[0] - b[0]
def sort_once():
    arr = (ctypes.c_int * 8)(*[8, 3, 5, 1, 7, 2, 6, 4])
    libc.qsort(arr, 8, ctypes.sizeof(ctypes.c_int), cmp)
    assert list(arr) == [1, 2, 3, 4, 5, 6, 7, 8], list(arr)
def main():
    loc.tag = "fiber"
    sort_once()
    require_migration(force_migrate())
    sort_once()
    runloom.sleep(0.01)
    sort_once()
    assert seen and all(t == "fiber" for t in seen), seen
    print("PASS", flush=True)
runloom.run(4, main)
''')


# ---------------------------------------------------------------------------
# Memory: cross-hub last-decrefs are queued to the allocating hub's own
# thread state, which never runs bytecode, so nothing merges them until a GC.
# (PR #23 review, 7.2)
# ---------------------------------------------------------------------------

def test_memory_cross_hub_g_handle_drop_frees_the_fiber_without_gc():
    """Known gap: a G handle dropped on another hub is brc-queued to that
    hub's tstate and its finished fiber (g + tstate + stack) survives until
    a GC; the hub loop must service its own merge queue.
    """
    assert_pass(r'''
import gc, collections
_watchdog(50)
gc.disable()
N = 600
def main():
    ch = runloom_c.Chan(64)
    cross = [0]
    def worker():
        ch.send((threading.get_ident(), runloom_c.current_g()))
    def collector():
        for _ in range(N):
            tid, h = ch.recv()[0]
            if tid != threading.get_ident():
                cross[0] += 1
            del h
    runloom.fiber(collector)
    for _ in range(N):
        runloom_c.mn_fiber(worker)
    # let every worker finish and the collector drain
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        runloom.sleep(0.05)
        states = collections.Counter(f.get("state") for f in runloom_c.fibers())
        if states.get("done", 0) == 0 and runloom_c.fiber_count() <= 2:
            break
    require_migration(cross[0] > 0)
    live = runloom_c.fiber_count()
    states = collections.Counter(f.get("state") for f in runloom_c.fibers())
    print("cross-hub drops=%d live_g=%d states=%s" % (cross[0], live, dict(states)), flush=True)
    assert states.get("done", 0) < N // 20, (
        "%d finished fibers still alive with no GC (cross-hub drops %d)"
        % (states.get("done", 0), cross[0]))
    print("PASS", flush=True)
runloom.run(4, main)
''')


def test_memory_waitgroup_fanout_frees_finished_fibers_without_gc():
    """Known gap: WaitGroup's contended CoFMutex parker holds a current_g()
    handle that another hub drops; each drop pins ~33 KiB of finished fiber
    until a GC (the 210 -> 977 MB observation).
    """
    assert_pass(r'''
import gc, collections
_watchdog(50)
gc.disable()
from runloom.sync import WaitGroup
PER, BATCHES = 1000, 3
def main():
    worst = 0
    for b in range(BATCHES):
        wg = WaitGroup()
        ch = runloom.Chan(0)
        def worker():
            try:
                ch.recv()
            finally:
                wg.done()
        for _ in range(PER):
            wg.add(1)
            runloom.fiber(worker)
        runloom.sleep(0.05)
        for _ in range(PER):
            ch.send(None)
        wg.wait()
        runloom.sleep(0.3)
        states = collections.Counter(f.get("state") for f in runloom_c.fibers())
        done = states.get("done", 0)
        worst = max(worst, done)
        print("batch %d: finished-but-alive fibers=%d states=%s" % (b, done, dict(states)), flush=True)
    assert worst < PER // 10, "%d finished fibers pinned with no GC" % worst
    print("PASS", flush=True)
runloom.run(4, main)
''')


# ---------------------------------------------------------------------------
# Scheduler: what the deleted sysmon preemption used to guarantee, and the
# races that came in with the global run-queue.  (PR #23 review, 7.4 and 7.5)
# ---------------------------------------------------------------------------

def test_preempt_cpu_bound_fiber_does_not_starve_its_hubs_timers():
    """Known gap: sysmon wall-clock preemption (default on in main) was
    deleted; a CPU-bound fiber monopolises its hub and every sleeper whose
    timer is on that hub stalls for the whole spin (main recovered in ~180
    ms).
    """
    assert_pass(r'''
_watchdog(40)
NS, SPIN = 8, 1.5
stall = [0.0] * NS
def spinner():
    t_end = time.monotonic() + SPIN
    while time.monotonic() < t_end:
        pass
def sleeper(i):
    t_end = time.monotonic() + SPIN + 0.3
    last = time.monotonic()
    while time.monotonic() < t_end:
        runloom.sleep(0.01)
        now = time.monotonic()
        stall[i] = max(stall[i], now - last)
        last = now
def main():
    for i in range(NS):
        runloom.fiber(sleeper, i)
    runloom.sleep(0.2)          # sleepers are spread over both hubs' heaps
    runloom.fiber(spinner)
    runloom.sleep(SPIN + 1.0)
    worst = max(stall)
    print("max sleeper stall %.0f ms (spin %.1f s)" % (worst * 1000, SPIN), flush=True)
    assert worst < SPIN / 2, "a sleeper stalled %.0f ms behind a CPU-bound fiber" % (worst * 1000)
    print("PASS", flush=True)
runloom.run(2, main)
''')


def test_sched_timer_pop_survives_a_concurrent_introspection_sweep():
    """Known gap: the timer pop CASes PARKED->RUNNING and drops the fiber when
    introspection holds it SWEEPING; the sleeper is then off the heap and
    on no queue for good.
    """
    assert_pass(r'''
import random
from runloom import inspect as swi
_watchdog(40)
N, DUR = 128, 2.0
done = bytearray(N)
def sleeper(i):
    rnd = random.Random(i)
    t_end = time.monotonic() + DUR
    while time.monotonic() < t_end:
        runloom.sleep(rnd.uniform(0.001, 0.005))
    done[i] = 1
def dumper():
    t_end = time.monotonic() + DUR + 0.2
    while time.monotonic() < t_end:
        swi.fibers(stacks=True)
        runloom.sleep(0)
def main():
    for i in range(N):
        runloom.fiber(sleeper, i)
    runloom.fiber(dumper)
    runloom.sleep(DUR + 1.5)
    finished = sum(done)
    print("finished %d/%d sleepers under continuous introspection" % (finished, N), flush=True)
    if finished != N:
        print("AssertionError: %d sleepers never woke again" % (N - finished), flush=True)
        os._exit(1)  # lost sleepers would otherwise wedge run() forever
    print("PASS", flush=True)
    os._exit(0)
runloom.run(4, main)
''')


def test_sched_single_thread_fibers_stay_isolated_while_mn_is_live():
    """Known gap: runloom_per_g_tstate_mode is process-global, so while an M:N
    run is live a single-thread scheduler on another OS thread skips the
    snap and context copy and its fibers share ContextVars and exc_info.
    """
    assert_pass(r'''
import contextvars
_watchdog(40)
N = 8
cv = contextvars.ContextVar("cv", default=None)
results, exc_results = {}, {}
def st_fiber(i):
    cv.set(i)
    runloom_c.sched_sleep(0.01)
    results[i] = cv.get()
    try:
        raise ValueError(i)
    except ValueError:
        runloom_c.sched_sleep(0.01)
        e = sys.exc_info()[1]
        exc_results[i] = e.args[0] if e is not None else None
def st_thread():
    for i in range(N):
        runloom_c.fiber(lambda i=i: st_fiber(i))
    runloom_c.run()
mn_started, mn_stop = threading.Event(), threading.Event()
def mn_main():
    mn_started.set()
    while not mn_stop.is_set():
        runloom.sleep(0.005)
t = threading.Thread(target=lambda: runloom.run(4, mn_main), daemon=True)
t.start(); mn_started.wait(); time.sleep(0.05)
st = threading.Thread(target=st_thread); st.start(); st.join()
mn_stop.set(); t.join(5)
got = [results.get(i) for i in range(N)]
exc = [exc_results.get(i) for i in range(N)]
print("contextvars=%s exc_info=%s" % (got, exc), flush=True)
assert got == list(range(N)), "single-thread fibers share a context while M:N is live: %s" % got
assert exc == list(range(N)), "exc_info lost across a sleep while M:N is live: %s" % exc
print("PASS", flush=True)
''')


def test_sched_mn_fini_frees_a_fiber_left_on_the_global_runq():
    """Known gap: mn_fini drains the global run-queue with one decref per
    entry but a queued g holds two refs, so a fiber woken just before an
    early run() exit leaks with its PyThreadState.
    """
    assert_pass(r'''
import signal, gc
_watchdog(40)
class Alarm(Exception): pass
def handler(signum, frame): raise Alarm()
signal.signal(signal.SIGALRM, handler)
ch, forever = runloom.Chan(0), runloom.Chan(0)
def parker(): ch.recv()
def spinner():
    t_end = time.monotonic() + 2.0
    while time.monotonic() < t_end:
        pass
def feeder():
    time.sleep(0.5)
    foreign_send(ch, 1)             # foreign-thread send: wake_g -> global runq
    time.sleep(0.2)
    signal.setitimer(signal.ITIMER_REAL, 0.01, 0)   # handler raises inside mn_run
def main():
    runloom.fiber(parker)
    runloom.sleep(0.05)
    runloom.fiber(spinner); runloom.fiber(spinner)   # both hubs busy: nothing pulls the runq
    threading.Thread(target=feeder, daemon=True).start()
    forever.recv()
try:
    runloom.run(2, main)
    how = "returned"
except Alarm:
    how = "Alarm carried out of run()"
gc.collect()
live = [g["state"] for g in runloom_c.fibers()]
print("run() %s; fibers alive after run(): %s" % (how, live), flush=True)
assert "submitted" not in live, "a run-queued fiber survived run(): %s" % live
print("PASS", flush=True)
os._exit(0)
''')


# ---------------------------------------------------------------------------
# OS-thread identity: stdlib and C code that key on the OS thread break once a
# fiber moves.  These need a design answer (a per-fiber pin, or a fiber-aware
# get_ident), not a patch; the tests track the gap.  (PR #23 review, 7.3)
# ---------------------------------------------------------------------------

def test_identity_stock_rlock_releases_after_a_migration():
    """Known gap: stock _thread.RLock keys ownership on the OS thread; after a
    migration release() raises and the lock is wedged for good (needs a
    per-fiber pin or a fiber-aware identity).
    """
    assert_pass(r'''
_watchdog(30)
def main():
    lk = threading.RLock()
    lk.acquire()
    require_migration(force_migrate())
    lk.release()                   # RuntimeError: cannot release un-acquired lock
    assert lk.acquire(timeout=1), "lock wedged after migration"
    lk.release()
    print("PASS", flush=True)
runloom.run(4, main)
''')


def test_identity_sqlite_connection_works_after_a_migration():
    """Known gap: sqlite3's default check_same_thread=True compares the OS
    thread; a connection used after a migration raises ProgrammingError.
    """
    assert_pass(r'''
import sqlite3
_watchdog(40)
N = 16
def main():
    ch = runloom.Chan(N)
    res = {"ok": 0, "fail": 0, "moved": 0}
    def worker(i):
        con = sqlite3.connect(":memory:")
        if force_migrate(rounds=20):
            res["moved"] += 1
        try:
            con.execute("select 1").fetchone(); res["ok"] += 1
        except sqlite3.ProgrammingError:
            res["fail"] += 1
        ch.send(i)
    for i in range(N):
        runloom.fiber(worker, i)
    for _ in range(N):
        ch.recv()
    print("ok=%d fail=%d migrated=%d" % (res["ok"], res["fail"], res["moved"]), flush=True)
    require_migration(res["moved"] > 0)
    assert res["fail"] == 0, "%d of %d migrated fibers lost their sqlite connection" % (res["fail"], res["moved"])
    print("PASS", flush=True)
runloom.run(4, main)
''')


def test_memory_cross_hub_bytes_drop_frees_them_without_gc():
    """A producer allocates 1 MiB buffers (alloc-home: owned by the thread it
    runs on) and a consumer on another hub drops them: every such drop is a
    non-owner last decref.  Without a per-fiber pin the hubs are not chosen,
    so each buffer carries the allocating thread id and the test counts how
    many drops were cross-hub; RSS must not grow with that count.

    Known gap: a large bytes object allocated on one hub and dropped on
    another is brc-queued to the allocating hub's tstate; bytes never
    advance the GC counters, so nothing frees it and RSS grows without
    bound.
    """
    assert_pass(r'''
import gc, resource
_watchdog(50)
gc.disable()
N, SIZE = 300, 1 << 20
def rss_mb():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1 << 20) if sys.platform == "darwin" else r / 1024.0
def main():
    ch = runloom.Chan(4)
    done = runloom.Chan(1)
    cross = [0]
    def producer():
        for i in range(N):
            ch.send((threading.get_ident(), bytes(SIZE)))
        ch.send(None)
    def consumer():
        try:
            while True:
                item, _ = ch.recv()
                if item is None:
                    break
                tid, buf = item
                if tid != threading.get_ident():
                    cross[0] += 1
                del buf, item
        finally:
            done.send(1)
    base = rss_mb()
    runloom.fiber(producer)
    runloom.fiber(consumer)
    done.recv()
    grew = rss_mb() - base
    print("cross-hub drops=%d of %d; peak RSS grew %.0f MiB" % (cross[0], N, grew), flush=True)
    require_migration(cross[0] >= N // 10)
    assert grew < cross[0] * SIZE / (1 << 20) / 4, (
        "%.0f MiB retained over %d cross-hub drops: never freed without a GC" % (grew, cross[0]))
    print("PASS", flush=True)
runloom.run(4, main)
''')


def test_identity_module_import_lock_releases_after_a_migration():
    """Known gap: importlib's _ModuleLock keys its owner on
    _thread.get_ident(); an importer that parks and migrates inside the
    module body cannot release the lock and every other importer of that
    module hangs.
    """
    assert_pass(r'''
import tempfile, importlib
_watchdog(15)
d = tempfile.mkdtemp()
with open(os.path.join(d, "parks_on_import.py"), "w") as f:
    f.write("import __main__\n__main__.MOVED = __main__.force_migrate()\nREADY = True\n")
sys.path.insert(0, d)
MOVED = False
def main():
    res = runloom.Chan(2)
    def importer(i):
        try:
            m = importlib.import_module("parks_on_import")
            res.send((i, m.READY, None))
        except BaseException as e:
            res.send((i, None, repr(e)))
    runloom.fiber(importer, 0)
    runloom.sleep(0)          # first importer is inside the module body now
    runloom.fiber(importer, 1)
    got = sorted(res.recv()[0] for _ in range(2))
    print("importers: %r moved=%s" % (got, MOVED), flush=True)
    require_migration(MOVED)
    assert got == [(0, True, None), (1, True, None)], got
    print("PASS", flush=True)
runloom.run(4, main)
''')


def test_identity_current_frames_lists_the_running_fiber_under_get_ident():
    """Known gap: a fiber's tstate->thread_id is its SPAWNER's thread while
    get_ident() is the current hub, so sys._current_frames() never lists
    the running fiber under the ident it reports (debuggers and
    faulthandler tooling look it up there).
    """
    assert_pass(r'''
_watchdog(30)
def main():
    require_migration(force_migrate())
    ident = threading.get_ident()
    frames = sys._current_frames()
    names = []
    f = frames.get(ident)
    while f is not None:
        names.append(f.f_code.co_name); f = f.f_back
    print("ident=%d current_thread.ident=%s frames_for_ident=%s"
          % (ident, threading.current_thread().ident, names), flush=True)
    assert threading.current_thread().ident == ident, "current_thread() is not the thread we run on"
    assert "main" in names, "the running fiber is missing from sys._current_frames()"
    print("PASS", flush=True)
runloom.run(4, main)
''')


def test_sched_offload_hub_never_pulls_unpinned_general_work():
    """H=2 general hubs kept busy by spinners while a foreign thread wakes a
    general fiber: the entry sits in the global run-queue and the only idle
    puller is the reserved offload hub.  The offload hub's OS thread is
    learnt from an offload_fiber, which can only run there.

    Known gap: the global run-queue pull accepts any unpinned entry (p1 ==
    0 || p1 == want), so an idle OFFLOAD hub takes general work and strands
    it behind its next blocking call.
    """
    assert_pass(r'''
_watchdog(40)
GEN, SPIN, ROUNDS = 2, 1.5, 6
def main():
    ch, res, off = runloom.Chan(0), runloom.Chan(1), runloom.Chan(1)
    runloom_c.offload_fiber(lambda: off.send(threading.get_ident()))
    offload_tid, _ = off.recv()
    def parker():
        seen = []
        for _ in range(ROUNDS):
            ch.recv()
            seen.append(threading.get_ident())
        res.send(seen)
    def spinner():
        t_end = time.monotonic() + SPIN
        while time.monotonic() < t_end:
            pass
    def feeder():
        time.sleep(0.2)
        for _ in range(ROUNDS):
            foreign_send(ch, 1); time.sleep(0.1)
    runloom.fiber(parker)
    runloom.sleep(0.05)
    for _ in range(GEN):
        runloom.fiber(spinner)
    threading.Thread(target=feeder, daemon=True).start()
    seen, _ = res.recv()
    on_offload = sum(1 for t in seen if t == offload_tid)
    print("hubs=%d offload=%d; parker resumed on the offload hub %d of %d times"
          % (runloom_c.mn_hub_count(), runloom_c.offload_hub_count(), on_offload, ROUNDS), flush=True)
    assert on_offload == 0, "general work ran on the offload hub %d of %d times" % (on_offload, ROUNDS)
    print("PASS", flush=True)
runloom.run(GEN, main, offload_hubs=1)
''')


def test_sched_foreign_thread_wake_reaches_a_shallow_idle_hub_promptly():
    """Known gap: a foreign-thread wake reaches a parked hub only through
    wakep_one, which fires once the idle wait exceeds 2 ms; at a faster
    cadence the fiber waits for the next 1 ms idle pump (p99 380-600 us vs
    11-38 us on the per-hub scheduler).
    """
    assert_pass(r'''
_watchdog(40)
N = 1500
def main():
    ch = runloom.Chan(0)
    def feeder():
        for i in range(N):
            time.sleep(0.001)
            while True:                 # stamp each attempt: a retry is a new send
                try:
                    ch.send(time.perf_counter_ns()); break
                except RuntimeError:    # receiver not parked yet; a thread cannot block
                    time.sleep(0.0002)
    threading.Thread(target=feeder, daemon=True).start()
    lat = []
    for _ in range(N):
        sent, _ = ch.recv()
        lat.append((time.perf_counter_ns() - sent) / 1000.0)
    lat.sort()
    p50, p99 = lat[len(lat) // 2], lat[int(len(lat) * 0.99)]
    print("foreign-thread wake latency p50=%.0f us p99=%.0f us" % (p50, p99), flush=True)
    assert p99 < 200, "p99 wake latency %.0f us" % p99
    print("PASS", flush=True)
runloom.run(4, main)
''')


# ---------------------------------------------------------------------------
# Cost: one PyThreadState per fiber.  (PR #23 review, 7.5)
# ---------------------------------------------------------------------------

def _cost(code, timeout=90):
    """Run a cost scenario twice, migration on then off, and return the two
    COST= values.  The bound is a RATIO, so it means the same thing on a
    laptop and on a 3-core CI runner; the on-run must observe a migration
    or the pair says nothing and the test skips."""
    pair = []
    for migration in (True, False):
        rc, out, err = run_scenario(code, timeout=timeout, migration=migration)
        if migration and "NOMIG" in out:
            pytest.skip("no cross-hub migration observed on this machine")
        m = re.search(r"COST=([0-9.eE+-]+)", out)
        if rc != 0 or m is None:
            print("--- scenario stdout (migration=runloom) ---\nrunloom\n--- stderr ---\nrunloom"
                  % (migration, out, err))
            pytest.fail("migration=runloom rc=runloom: runloom" % (migration, rc, _key_line(out, err)),
                        pytrace=False)
        pair.append(float(m.group(1)))
    return pair[0], pair[1]


# Each cost scenario prints COST=<number>.  Under migration it first proves a
# migration is possible (else NOMIG); with migration off that probe is skipped.
_PROBE = '''
if os.environ.get("RUNLOOM_MIGRATION") == "1":
    def _probe():
        require_migration(force_migrate())
    runloom.run(4, _probe)
'''

GC_COLLECT_COST = _PROBE + r'''
import gc
_watchdog(80)
N = 5000
def best_collect(k=5):
    best = 1e9
    for _ in range(k):
        t0 = time.perf_counter(); gc.collect(); best = min(best, time.perf_counter() - t0)
    return best
def main():
    empty = best_collect()
    ch = runloom.Chan(0)
    for _ in range(N):
        runloom.fiber(ch.recv)
    runloom.sleep(0.5)
    full = best_collect()
    per = (full - empty) / N * 1e6
    print("gc.collect(): %.2f ms empty, %.2f ms with %d parked" % (empty * 1e3, full * 1e3, N), flush=True)
    print("COST=%.4f" % per, flush=True)
    for _ in range(N):
        ch.send(None)
runloom.run(4, main)
'''

PARKED_FIBER_RSS = _PROBE + r'''
import resource
_watchdog(80)
N = 4000
def rss_kib():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024.0 if sys.platform == "darwin" else float(r)
def main():
    ch = runloom.Chan(0)
    for _ in range(200):               # warm the slabs and the stack pool
        runloom.fiber(ch.recv)
    runloom.sleep(0.2)
    base = rss_kib()
    for _ in range(N):
        runloom.fiber(ch.recv)
    runloom.sleep(0.5)
    per = (rss_kib() - base) / N
    print("COST=%.3f" % per, flush=True)
    for _ in range(N + 200):
        ch.send(None)
runloom.run(4, main)
'''

SPAWN_COST = _PROBE + r'''
_watchdog(80)
N = 20000
def main():
    ch = runloom.Chan(N)
    def w(): ch.send(1)
    best = 1e9
    for _ in range(3):
        t0 = time.perf_counter()
        for _ in range(N): runloom.fiber(w)
        for _ in range(N): ch.recv()
        best = min(best, time.perf_counter() - t0)
    print("COST=%.4f" % (best / N * 1e6), flush=True)
runloom.run(4, main)
'''


def test_cost_gc_collect_per_parked_fiber_within_2x_of_migration_off():
    """Known gap: gc.collect() visits every parked fiber's own tstate (about
    0.45 us each on macOS, 4x the per-hub scheduler at 20k parked).
    """
    on, off = _cost(GC_COLLECT_COST)
    print("gc.collect() per parked fiber: %.3f us with migration, %.3f us without" % (on, off))
    assert on < 2 * off, "gc.collect() %.3f us per parked fiber vs %.3f us without migration (%.1fx)" % (on, off, on / off)


def test_cost_parked_fiber_rss_within_1_5x_of_migration_off():
    """Known gap: a parked fiber carries its own PyThreadState and its 16 KiB
    datastack chunk (33 KiB RSS against 17 KiB without migration on macOS;
    19 KiB with on Linux).
    """
    on, off = _cost(PARKED_FIBER_RSS)
    print("RSS per parked fiber: %.1f KiB with migration, %.1f KiB without" % (on, off))
    assert on < 1.5 * off, "%.1f KiB per parked fiber vs %.1f KiB without migration (%.2fx)" % (on, off, on / off)


def test_cost_spawn_and_complete_within_2x_of_migration_off():
    """Known gap: PyThreadState_New per spawn (2.6 us per spawn+complete at
    H=4 on macOS against 0.4 us on the per-hub scheduler).
    """
    on, off = _cost(SPAWN_COST)
    print("spawn+complete: %.2f us with migration, %.2f us without" % (on, off))
    assert on < 2 * off, "spawn+complete %.2f us vs %.2f us without migration (%.1fx)" % (on, off, on / off)


# ---------------------------------------------------------------------------
# Not reproduced: the signal-wake heap race (PR #23 review, 7.5 #6) is confirmed by
# reading only -- the window is ~100 ns per delivery.  This stress drives the
# path (a raising SIGALRM handler is delivered INTO a parked io-sleeper, which
# is woken from the main thread through the global run-queue, resumes on some
# other hub and edits its ORIGIN hub's sleep heap while that hub pops timers)
# and passes today; it is here so a crash or a lost sleeper has a name.  Each
# hub holds one undelivered exception at a time, so a burst can find every
# slot full and carry the exception out of run(): that ends the stress early
# and is tolerated, a crash or a lost churner is not.
# ---------------------------------------------------------------------------

def test_sched_signal_woken_io_sleeper_survives_origin_heap_churn():
    assert_pass(r'''
import signal
_watchdog(40)
NS, DUR = 64, 2.5
done = bytearray(NS)
hits, delivered, completed = [0], [0], [False]
class Tick(Exception): pass
def handler(signum, frame):
    hits[0] += 1
    raise Tick()
signal.signal(signal.SIGALRM, handler)      # main thread, before run()
def churner(i):
    t_end = time.monotonic() + DUR
    while time.monotonic() < t_end:
        runloom.sleep(0.001)
    done[i] = 1
def recipient():
    t_end = time.monotonic() + DUR
    while time.monotonic() < t_end:
        try:
            runloom_c.sched_sleep_io(0.01)
        except Tick:
            delivered[0] += 1
def main():
    for i in range(NS):
        runloom.fiber(churner, i)
    for _ in range(8):
        runloom.fiber(recipient)
    runloom.sleep(0.1)                   # recipients are parked
    signal.setitimer(signal.ITIMER_REAL, 0.001, 0.001)
    t_end = time.monotonic() + DUR + 0.5
    while time.monotonic() < t_end:
        try:
            runloom.sleep(t_end - time.monotonic())
        except Tick:
            pass
    signal.setitimer(signal.ITIMER_REAL, 0, 0)
    completed[0] = True
try:
    runloom.run(4, main)
except Tick:
    signal.setitimer(signal.ITIMER_REAL, 0, 0)
print("signals=%d delivered into io-sleepers=%d run completed=%s finished %d/%d churners"
      % (hits[0], delivered[0], completed[0], sum(done), NS), flush=True)
assert delivered[0] >= 50, "only %d signals reached a parked io-sleeper" % delivered[0]
if completed[0]:
    assert sum(done) == NS, "%d sleepers lost" % (NS - sum(done))
print("PASS", flush=True)
''')


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
