"""What must hold for a fiber after it migrates to another hub.

A migrated fiber must free what it drops, keep the scheduler's timers and
queues intact, still be the same fiber to code keyed on the OS thread, and
not cost much more than a fiber that never moved.  Each test here states
one such invariant; the failing ones are the known gaps of migration mode,
left red on purpose until each is closed.

Every M:N fiber carries its own PyThreadState and a woken fiber may resume
on any hub.  The rest of the suite rarely exercises that --
``stackweave.sleep()`` always resumes on the owning hub, and a wake performed
by another fiber lands on the WAKER's own deque (Go-style local wake), so a
channel hand-off between fibers stays on one hub -- so these tests FORCE a
migration.  A plain OS thread sends on the channel: a foreign waker has no
deque, so the fiber goes through the global run-queue and whichever hub is
idle pulls it.  The thread id is checked before and after each park, and a
scenario that never sees a migration skips loudly rather than passing.

Each scenario runs in a fresh subprocess so a lost fiber or a wedged hub is a
clean timeout rather than a hung pytest, and so one scenario's leak cannot
leak into the next.

Fifteen gaps are closed (their docstrings start "Was a gap").  The five
that remain are strict xfails under TODO_MIGRATION_FAIL: four OS-thread-identity
checks that live in C or in importlib and need a pin or a monkey patch (one,
the same-hub importer, needs no migration at all: every fiber on a hub shares
its thread id), and a timer wake that ignores G.pin.  A strict xfail still
runs, and flips to a hard XPASS failure the moment its gap is closed.  The cost of one PyThreadState per fiber (gc.collect() per parked
fiber, RSS per parked fiber, spawn) was bounded against the per-hub
scheduler while that scheduler existed; with migration the only mode there
is no in-tree baseline, so those three comparisons are not carried here.

Names are ``test_<area>_<invariant>``.  The area is the subsystem a fix
lands in, so ``-k memory`` (or identity, sched, preempt, cost, harness)
selects one gap family:

    harness   the file's own premise (migration is observable)
    memory    the brc merge queue: cross-hub last decrefs that never run,
              or that run on the old hub racing the fiber
    preempt   what the deleted sysmon wall-clock preemption guaranteed
    sched     races and leaks that came in with the global run-queue
    identity  code keyed on the OS thread after a fiber has moved
    cost      the price of one PyThreadState per fiber (bound in the name)

The invariant is the behaviour that must hold, not the bug, so the name
reads as a plain guard once the gap is closed.  The gaps that older
tests already cover when run with migration on (hub introspection, sysmon
classification, the stack pool, the seeded scheduler) are not duplicated
here.

Four tests pass today and are here to pin down what was verified to work:
that migration is actually observable through this harness, that
``PyGILState_Ensure`` callbacks (ctypes, sqlite UDFs, OpenSSL) run on the
fiber's own thread state after it has moved, that ``G.pin`` keeps a fiber
on one OS thread, and that signal delivery into a migrating io-sleeper
neither crashes nor cuts the fiber's next sleep short.
"""
import ast
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
import stackweave, stackweave_c

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
    ch = stackweave.Chan(0)
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
    ch = stackweave.Chan(0)
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


def run_scenario(code, timeout=60, env=None):
    env = dict(os.environ, **(env or {}))
    env["PYTHON_GIL"] = "0"
    env["STACKWEAVE_GIL"] = "0"
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
        if re.match(r"\s*(\w+(Error|Exception|Interrupt)\b|WATCHDOG|\[STACKWEAVE_DEBUG=)", line):
            return line.strip()
    lines = [l for l in out.splitlines() if l.strip()]
    return lines[-1].strip() if lines else "(no output)"


def assert_pass(code, timeout=60, env=None):
    rc, out, err = run_scenario(code, timeout=timeout, env=env)
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



def TODO_MIGRATION_FAIL(reason, raises=None):
    """A known gap of migration mode that stays open on purpose: the test is a
    strict xfail, so it still runs in CI, shows as xfailed, and the moment the
    gap is closed it turns into a hard XPASS failure that forces this marker
    off.  Grep TODO_MIGRATION_FAIL for the open list.  With
    raises=AssertionError only the behaviour check counts as the gap, and a
    scenario that calls pytest.fail (it never reached its trigger) fails."""
    return pytest.mark.xfail(strict=True, raises=raises,
                             reason="TODO_MIGRATION_FAIL: " + reason)

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
stackweave.run(4, main)
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
    stackweave.sleep(0.01)
    sort_once()
    assert seen and all(t == "fiber" for t in seen), seen
    print("PASS", flush=True)
stackweave.run(4, main)
''')


def test_identity_pinned_fiber_stays_on_one_os_thread():
    """G.pin(N) is the escape hatch for code keyed on the OS thread.  A fiber
    pinned to the hub it is on must resume there after every wake, so a stock
    RLock and a default sqlite3 connection keep working across parks.  The
    unpinned control in the same run must see at least one other thread, or
    the pin proved nothing."""
    assert_pass(r'''
import sqlite3
_watchdog(40)
def main():
    control = park_n_times(12)
    require_migration(len(control) > 1)
    g = stackweave_c.current_g()
    g.pin(stackweave_c.mn_current_hub())
    lk = threading.RLock()
    con = sqlite3.connect(":memory:")
    lk.acquire()
    pinned = park_n_times(12)
    lk.release()
    assert con.execute("select 1").fetchone() == (1,)
    print("unpinned threads=%d pinned threads=%d" % (len(control), len(pinned)), flush=True)
    assert len(pinned) == 1, "a pinned fiber resumed on %d different OS threads" % len(pinned)
    g.pin(None)
    print("PASS", flush=True)
stackweave.run(4, main)
''')


# ---------------------------------------------------------------------------
# Memory: cross-hub last-decrefs are queued to the allocating hub's own
# thread state, which never runs bytecode, so nothing merges them until a GC.
# (PR #23 review, 7.2)
# ---------------------------------------------------------------------------

def test_memory_cross_hub_g_handle_drop_frees_the_fiber_without_gc():
    """Was a gap (fixed: the hub services its brc merge queue and the running fiber receives the hub's drops): a G handle dropped on another hub is brc-queued to that
    hub's tstate and its finished fiber (g + tstate + stack) survives until
    a GC; the hub loop must service its own merge queue.
    """
    assert_pass(r'''
import gc, collections
_watchdog(50)
gc.disable()
N = 600
def main():
    ch = stackweave_c.Chan(64)
    cross = [0]
    def worker():
        ch.send((threading.get_ident(), stackweave_c.current_g()))
    def collector():
        for _ in range(N):
            tid, h = ch.recv()[0]
            if tid != threading.get_ident():
                cross[0] += 1
            del h
    stackweave.fiber(collector)
    for _ in range(N):
        stackweave_c.mn_fiber(worker)
    # let every worker finish and the collector drain
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        stackweave.sleep(0.05)
        states = collections.Counter(f.get("state") for f in stackweave_c.fibers())
        if states.get("done", 0) == 0 and stackweave_c.fiber_count() <= 2:
            break
    require_migration(cross[0] > 0)
    live = stackweave_c.fiber_count()
    states = collections.Counter(f.get("state") for f in stackweave_c.fibers())
    print("cross-hub drops=%d live_g=%d states=%s" % (cross[0], live, dict(states)), flush=True)
    assert states.get("done", 0) < N // 20, (
        "%d finished fibers still alive with no GC (cross-hub drops %d)"
        % (states.get("done", 0), cross[0]))
    print("PASS", flush=True)
stackweave.run(4, main)
''')


def test_memory_waitgroup_fanout_frees_finished_fibers_without_gc():
    """Was a gap (fixed: the hub services its brc merge queue and the running fiber receives the hub's drops): WaitGroup's contended CoFMutex parker holds a current_g()
    handle that another hub drops; each drop pins ~33 KiB of finished fiber
    until a GC (the 210 -> 977 MB observation).
    """
    assert_pass(r'''
import gc, collections
_watchdog(50)
gc.disable()
from stackweave.sync import WaitGroup
PER, BATCHES = 1000, 3
def main():
    worst = 0
    for b in range(BATCHES):
        wg = WaitGroup()
        ch = stackweave.Chan(0)
        def worker():
            try:
                ch.recv()
            finally:
                wg.done()
        for _ in range(PER):
            wg.add(1)
            stackweave.fiber(worker)
        stackweave.sleep(0.05)
        for _ in range(PER):
            ch.send(None)
        wg.wait()
        stackweave.sleep(0.3)
        states = collections.Counter(f.get("state") for f in stackweave_c.fibers())
        done = states.get("done", 0)
        worst = max(worst, done)
        print("batch %d: finished-but-alive fibers=%d states=%s" % (b, done, dict(states)), flush=True)
    assert worst < PER // 10, "%d finished fibers pinned with no GC" % worst
    print("PASS", flush=True)
stackweave.run(4, main)
''')


# ---------------------------------------------------------------------------
# Scheduler: what the deleted sysmon preemption used to guarantee, and the
# races that came in with the global run-queue.  (PR #23 review, 7.4 and 7.5)
# ---------------------------------------------------------------------------

def test_preempt_cpu_bound_fiber_does_not_starve_its_hubs_timers():
    """Was a gap (fixed: sysmon classifies the RUNNING fiber's tstate, read through a hazard pointer): sysmon wall-clock preemption (default on in main) was
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
        stackweave.sleep(0.01)
        now = time.monotonic()
        stall[i] = max(stall[i], now - last)
        last = now
def main():
    for i in range(NS):
        stackweave.fiber(sleeper, i)
    stackweave.sleep(0.2)          # sleepers are spread over both hubs' heaps
    stackweave.fiber(spinner)
    stackweave.sleep(SPIN + 1.0)
    worst = max(stall)
    print("max sleeper stall %.0f ms (spin %.1f s)" % (worst * 1000, SPIN), flush=True)
    assert worst < SPIN / 2, "a sleeper stalled %.0f ms behind a CPU-bound fiber" % (worst * 1000)
    print("PASS", flush=True)
stackweave.run(2, main)
''')


def test_sched_timer_pop_survives_a_concurrent_introspection_sweep():
    """Was a gap (fixed: the timer pop hands a SWEEPING sleeper to the sweeper (SWEEPING -> SWEEPING_WOKEN)): the timer pop CASes PARKED->RUNNING and drops the fiber when
    introspection holds it SWEEPING; the sleeper is then off the heap and
    on no queue for good.
    """
    assert_pass(r'''
import random
from stackweave import inspect as swi
_watchdog(40)
N, DUR = 128, 2.0
done = bytearray(N)
def sleeper(i):
    rnd = random.Random(i)
    t_end = time.monotonic() + DUR
    while time.monotonic() < t_end:
        stackweave.sleep(rnd.uniform(0.001, 0.005))
    done[i] = 1
def dumper():
    t_end = time.monotonic() + DUR + 0.2
    while time.monotonic() < t_end:
        swi.fibers(stacks=True)
        stackweave.sleep(0)
def main():
    for i in range(N):
        stackweave.fiber(sleeper, i)
    stackweave.fiber(dumper)
    stackweave.sleep(DUR + 1.5)
    finished = sum(done)
    print("finished %d/%d sleepers under continuous introspection" % (finished, N), flush=True)
    if finished != N:
        print("AssertionError: %d sleepers never woke again" % (N - finished), flush=True)
        os._exit(1)  # lost sleepers would otherwise wedge run() forever
    print("PASS", flush=True)
    os._exit(0)
stackweave.run(4, main)
''')


def test_sched_single_thread_fibers_stay_isolated_while_mn_is_live():
    """Was a gap (fixed: the per-g gates apply only inside an M:N hub (runloom_mn_current_sched)): runloom_per_g_tstate_mode is process-global, so while an M:N
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
    stackweave_c.sched_sleep(0.01)
    results[i] = cv.get()
    try:
        raise ValueError(i)
    except ValueError:
        stackweave_c.sched_sleep(0.01)
        e = sys.exc_info()[1]
        exc_results[i] = e.args[0] if e is not None else None
def st_thread():
    for i in range(N):
        stackweave_c.fiber(lambda i=i: st_fiber(i))
    stackweave_c.run()
mn_started, mn_stop = threading.Event(), threading.Event()
def mn_main():
    mn_started.set()
    while not mn_stop.is_set():
        stackweave.sleep(0.005)
t = threading.Thread(target=lambda: stackweave.run(4, mn_main), daemon=True)
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
    """Was a gap (fixed: the fini drain drops the queue ref as well as the scheduler ref): mn_fini drains the global run-queue with one decref per
    entry but a queued g holds two refs, so a fiber woken just before an
    early run() exit leaks with its PyThreadState.
    """
    assert_pass(r'''
import signal, gc
_watchdog(40)
class Alarm(Exception): pass
def handler(signum, frame): raise Alarm()
signal.signal(signal.SIGALRM, handler)
ch, forever = stackweave.Chan(0), stackweave.Chan(0)
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
    stackweave.fiber(parker)
    stackweave.sleep(0.05)
    stackweave.fiber(spinner); stackweave.fiber(spinner)   # both hubs busy: nothing pulls the runq
    threading.Thread(target=feeder, daemon=True).start()
    forever.recv()
try:
    stackweave.run(2, main)
    how = "returned"
except Alarm:
    how = "Alarm carried out of run()"
gc.collect()
live = [g["state"] for g in stackweave_c.fibers()]
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

@TODO_MIGRATION_FAIL(
    'stock _thread.RLock compares the OS thread id in C at release; nothing in the runtime can satisfy it once the fiber moved -- use G.pin or monkey.patch() (CoRLock)')
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
stackweave.run(4, main)
''')


@TODO_MIGRATION_FAIL(
    'sqlite3 check_same_thread compares the OS thread id in C; use check_same_thread=False or G.pin, as for OS threads')
def test_identity_sqlite_connection_works_after_a_migration():
    """Known gap: sqlite3's default check_same_thread=True compares the OS
    thread; a connection used after a migration raises ProgrammingError.
    """
    assert_pass(r'''
import sqlite3
_watchdog(40)
N = 16
def main():
    ch = stackweave.Chan(N)
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
        stackweave.fiber(worker, i)
    for _ in range(N):
        ch.recv()
    print("ok=%d fail=%d migrated=%d" % (res["ok"], res["fail"], res["moved"]), flush=True)
    require_migration(res["moved"] > 0)
    assert res["fail"] == 0, "%d of %d migrated fibers lost their sqlite connection" % (res["fail"], res["moved"])
    print("PASS", flush=True)
stackweave.run(4, main)
''')


def test_memory_cross_hub_bytes_drop_frees_them_without_gc():
    """Producer pinned to hub 0 allocates 1 MiB buffers (alloc-home: owned by
    hub 0's thread id); a consumer pinned to hub 1 drops them.  Every drop is
    a non-owner last decref.  Peak RSS must stay near a handful of buffers.

    Was a gap (fixed: drops during a resume are routed to the running fiber's tstate, which merges them at its next eval-breaker check): a large bytes object allocated on one hub and dropped on
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
    ch = stackweave.Chan(4)
    tids = {"prod": None, "cons": set()}
    def producer():
        stackweave_c.current_g().pin(0)
        stackweave.sleep(0)
        tids["prod"] = threading.get_ident()
        for i in range(N):
            ch.send(bytes(SIZE))
        ch.send(None)
    def consumer():
        stackweave_c.current_g().pin(1)
        stackweave.sleep(0)
        while True:
            buf, _ = ch.recv()
            if buf is None:
                break
            tids["cons"].add(threading.get_ident())
            del buf
    done = stackweave.Chan(1)
    def consumer_then_done():
        try:
            consumer()
        finally:
            done.send(1)
    base = rss_mb()
    stackweave_c.mn_fiber(producer, hub=0)
    stackweave.fiber(consumer_then_done)
    done.recv()
    grew = rss_mb() - base
    print("cross-hub=%s peak RSS grew %.0f MiB over %d x 1 MiB drops"
          % (tids["cons"] != {tids["prod"]}, grew, N), flush=True)
    require_migration(tids["prod"] is not None and tids["cons"] and tids["cons"] != {tids["prod"]})
    assert grew < N * SIZE / (1 << 20) / 4, (
        "%.0f MiB retained: cross-hub bytes are never freed without a GC" % grew)
    print("PASS", flush=True)
stackweave.run(4, main)
''')


def _interpreter_has_mv_exports_fix():
    """Whether this interpreter carries the exec-home patch's memoryobject.c
    hunk.  That file is not installed, so the patch also defines
    _Py_MV_EXPORTS_ATOMIC in the installed object.h (3.14) or
    cpython/object.h (3.15); tools/ci/lib.sh checks the same witness."""
    import sysconfig
    include = sysconfig.get_path("include")
    for header in ("object.h", os.path.join("cpython", "object.h")):
        try:
            with open(os.path.join(include, header), errors="replace") as f:
                if "_Py_MV_EXPORTS_ATOMIC" in f.read():
                    return True
        except OSError:
            pass
    return False


def test_memory_memoryview_slice_survives_a_migration():
    """64 ping-pong pairs at H=8; each pinger holds `view[0:]` of its own
    memoryview across the channel park, then drops it.

    Was a gap (fixed in the exec-home CPython patch: the managed buffer's
    export count is updated atomically): a slice created on one hub and
    dropped on another is brc-queued back to the hub that allocated it, so
    its dealloc (`--mbuf->exports`) ran there while the fiber, now on
    another hub, was already taking the next slice (`mbuf->exports++`).
    The plain ++/-- lost updates: a lost increment released the buffer
    under `view` ("operation forbidden on released memoryview object"), a
    lost decrement pinned the bytearray for good.  Refcounts were never
    wrong.  The failure rate tracked OS-thread moves, not the hub count.
    Runs batches until MOVES_WANTED OS-thread moves were seen so a pass
    means the gap is really closed, not unexercised.

    The fix lives in the interpreter, so on one built from an older copy of
    src/patches/ (no _Py_MV_EXPORTS_ATOMIC witness in its headers) the
    scenario still runs, but a failure is an xfail asking for a rebuild.
    """
    try:
        _run_memoryview_slice_scenario()
    except pytest.fail.Exception:
        if _interpreter_has_mv_exports_fix():
            raise
        pytest.xfail("this interpreter lacks the exec-home memoryobject.c hunk "
                     "(no _Py_MV_EXPORTS_ATOMIC in its installed object.h): "
                     "rebuild the interpreter from src/patches/")


def _run_memoryview_slice_scenario():
    assert_pass(r'''
_watchdog(50)
PAIRS, ROUNDS, MOVES_WANTED, BUDGET_S = 64, 4000, 3000, 30
state = {"errors": [], "moves": 0}
def batch():
    wg = stackweave.WaitGroup()
    wg.add(PAIRS)
    for _ in range(PAIRS):
        a, b = stackweave_c.Chan(0), stackweave_c.Chan(0)
        def ponger(a=a, b=b):
            while True:
                v, ok = a.recv()
                if not ok:
                    return
                b.send(v)
        def pinger(a=a, b=b):
            view = memoryview(bytearray(64))
            tid = threading.get_ident()
            try:
                for r in range(ROUNDS):
                    sl = view[0:]
                    a.send(r)
                    b.recv()
                    del sl
                    t = threading.get_ident()
                    if t != tid:
                        state["moves"] += 1
                        tid = t
            except ValueError as e:
                state["errors"].append("round %d: %s" % (r, e))
            finally:
                a.close()
                wg.done()
        stackweave.fiber(ponger)
        stackweave.fiber(pinger)
    wg.wait()
def main():
    t0 = time.monotonic()
    while (not state["errors"] and state["moves"] < MOVES_WANTED
           and time.monotonic() - t0 < BUDGET_S):
        batch()
    print("moves=%d errors=%d %s" % (state["moves"], len(state["errors"]),
                                      state["errors"][:1]), flush=True)
    # A released view only ever follows a move, so an error proves migration.
    require_migration(state["errors"] or state["moves"] >= 100)
    assert not state["errors"], (
        "%d pinger(s) found their memoryview released after a migration: %s"
        % (len(state["errors"]), state["errors"][0]))
    print("PASS", flush=True)
stackweave.run(8, main)
''', timeout=90)


def _interpreter_has_array_exports_fix():
    """Whether this interpreter carries the exec-home patch's arraymodule.c
    hunk, by its installed witness _Py_ARRAY_EXPORTS_ATOMIC (see
    _interpreter_has_mv_exports_fix)."""
    import sysconfig
    include = sysconfig.get_path("include")
    for header in ("object.h", os.path.join("cpython", "object.h")):
        try:
            with open(os.path.join(include, header), errors="replace") as f:
                if "_Py_ARRAY_EXPORTS_ATOMIC" in f.read():
                    return True
        except OSError:
            pass
    return False


def test_memory_array_view_survives_a_migration():
    """64 ping-pong pairs at H=8; each pinger holds `memoryview(arr)` of its
    own array.array across the channel park, then drops it.

    Was a gap (fixed in the exec-home CPython patch: array.array's export
    count is updated atomically): a view created on one hub and dropped on
    another is brc-queued back to the hub that allocated it, so its release
    (`array_buffer_relbuf`, `ob_exports--`) ran there while the fiber, now
    on another hub, was already taking the next view (`array_buffer_getbuf`,
    `ob_exports++`).  Neither has a critical section, so the plain ++/--
    lost updates: a lost decrement pins the array for good ("cannot resize
    an array that is exporting buffers"), a lost increment lets it resize
    under a live view.  Same class as the memoryview count above; upstream
    too (gh-154524, plain threads on a stock 3.14.4t).

    After run() returns and a gc.collect() has merged every queued decref,
    each array must resize with no view live, and must refuse to while a
    fresh one is.  Runs batches until MOVES_WANTED OS-thread moves were
    seen so a pass means the gap is really closed, not unexercised.  On an
    interpreter without the hunk (no _Py_ARRAY_EXPORTS_ATOMIC witness) a
    failure is an xfail asking for a rebuild.

    A pinned array has a second cause, fixed in the runtime: a view whose
    deallocation was parked on a fiber's trashcan list and never run, so it
    never released its export (see
    test_cross_hub_drops_are_freed_under_the_hub_stacks_limits).
    """
    try:
        _run_array_view_scenario()
    except pytest.fail.Exception:
        if _interpreter_has_array_exports_fix():
            raise
        pytest.xfail("this interpreter lacks the exec-home arraymodule.c hunk "
                     "(no _Py_ARRAY_EXPORTS_ATOMIC in its installed object.h): "
                     "rebuild the interpreter from src/patches/")


def _run_array_view_scenario():
    assert_pass(r'''
import array, gc
_watchdog(50)
PAIRS, ROUNDS, MOVES_WANTED, BUDGET_S = 64, 4000, 3000, 30
state = {"moves": 0, "arrays": []}
def batch():
    wg = stackweave.WaitGroup()
    wg.add(PAIRS)
    for _ in range(PAIRS):
        a, b = stackweave_c.Chan(0), stackweave_c.Chan(0)
        def ponger(a=a, b=b):
            while True:
                v, ok = a.recv()
                if not ok:
                    return
                b.send(v)
        def pinger(a=a, b=b):
            arr = array.array("i", range(16))
            tid = threading.get_ident()
            try:
                for r in range(ROUNDS):
                    mv = memoryview(arr)
                    a.send(r)
                    b.recv()
                    del mv
                    t = threading.get_ident()
                    if t != tid:
                        state["moves"] += 1
                        tid = t
            finally:
                state["arrays"].append(arr)
                a.close()
                wg.done()
        stackweave.fiber(ponger)
        stackweave.fiber(pinger)
    wg.wait()
def main():
    t0 = time.monotonic()
    while state["moves"] < MOVES_WANTED and time.monotonic() - t0 < BUDGET_S:
        batch()
stackweave.run(8, main)
gc.collect()                       # merge every brc-queued view release
pinned = unguarded = 0
for arr in state["arrays"]:
    try:
        arr.append(0)              # no view is live: must resize
    except BufferError:
        pinned += 1                # lost decrement
        continue
    mv = memoryview(arr)
    try:
        arr.append(0)              # a view is live: must refuse
        unguarded += 1             # lost increment (count went negative)
    except BufferError:
        pass
    mv.release()
print("moves=%d arrays=%d pinned=%d unguarded=%d"
      % (state["moves"], len(state["arrays"]), pinned, unguarded), flush=True)
require_migration(pinned or unguarded or state["moves"] >= 100)
assert not pinned and not unguarded, (
    "%d of %d arrays kept a wrong export count after a migration "
    "(%d pinned: an export never released; %d resizable under a live view)"
    % (pinned + unguarded, len(state["arrays"]), pinned, unguarded))
print("PASS", flush=True)
''', timeout=90)


# Ping-pong pairs whose memoryviews are dropped on another hub than allocated
# them, until at least MERGES_WANTED parked fibers' queues were drained on their
# hub's stack (runloom_iframe_brc_release).  Shared by the drain invariants below.
_CROSS_HUB_DROP_WORKLOAD = r'''
_watchdog(50)
import array
PAIRS, ROUNDS, MERGES_WANTED, BUDGET_S = 32, 1500, 20, 30
state = {"moves": 0}
def batch():
    wg = stackweave.WaitGroup()
    wg.add(PAIRS)
    for _ in range(PAIRS):
        a, b = stackweave_c.Chan(0), stackweave_c.Chan(0)
        def ponger(a=a, b=b):
            while True:
                v, ok = a.recv()
                if not ok:
                    return
                b.send(v)
        def pinger(a=a, b=b):
            arr = array.array("i", range(16))
            tid = threading.get_ident()
            try:
                for r in range(ROUNDS):
                    mv = memoryview(arr)
                    a.send(r)
                    b.recv()
                    del mv            # often on another hub than allocated it
                    t = threading.get_ident()
                    if t != tid:
                        state["moves"] += 1
                        tid = t
            finally:
                a.close()
                wg.done()
        stackweave.fiber(ponger)
        stackweave.fiber(pinger)
    wg.wait()
def main():
    t0 = time.monotonic()
    while (stackweave_c.stats()["brc_release_merges"] < MERGES_WANTED
           and time.monotonic() - t0 < BUDGET_S):
        batch()
stackweave.run(8, main)
'''


def test_cross_hub_drops_are_freed_under_the_hub_stacks_limits():
    """A fiber's last decref of an object another hub allocated is queued to
    that hub's current receiver -- often the fiber running there, whose
    queue the hub drains on its OWN stack once that fiber parks
    (runloom_iframe_brc_release), with the fiber's state still attached.
    That state's C-stack limits describe the fiber's coroutine stack, so
    every _Py_Dealloc in the drain measured the hub's stack pointer against
    them.  Wherever the hub's stack lay below the fiber's, the margin came
    out negative and each object was parked on the fiber's trashcan list
    instead of freed -- for good, once the fiber stopped deallocating: a
    leaked memoryview that pinned its array in
    test_memory_array_view_survives_a_migration on CI.  The drain now
    borrows the hub's limits.

    Whether a wrong drain LEAKS depends on where the stacks happen to lie,
    so this checks what holds on every drain instead: stats() counts the
    drains and those that ran with the stack pointer outside the attached
    state's C-stack window, and the second must stay 0.
    """
    assert_pass(_CROSS_HUB_DROP_WORKLOAD + r'''
st = stackweave_c.stats()
merges, off = st["brc_release_merges"], st["brc_release_merges_off_stack"]
print("moves=%d merges=%d off_stack=%d" % (state["moves"], merges, off), flush=True)
# Checked before anything that could skip: the invariant needs no fiber to
# move (a ponger on another hub drops onto the pinger's hub just the same).
assert off == 0, (
    "%d of %d drains ran deallocations with the stack pointer outside the "
    "attached state's C-stack window" % (off, merges))
assert merges > 0, "no parked fiber's queue was drained on its hub's stack"
print("PASS", flush=True)
''', timeout=90)


def test_cross_hub_drops_drain_before_the_hub_heads_the_bucket():
    """release() drains a parked fiber's biased-refcount queue while the
    FIBER's state still heads the hub's bucket, and only then hands the head
    back to the hub's state.

    The hub's state is detached for the whole resume.  From CPython 3.15.0rc3
    (gh-157838) a dropper that finds a DETACHED owner suspends it and merges
    its queue itself, rewriting ob_ref_local/ob_tid of objects that thread
    owns -- safe only because a suspended thread cannot touch them.  The hub's
    thread can: the drain runs deallocators under the fiber's state, which
    shares the hub's thread id.  With the hub at the head during the drain,
    the two race on the same objects' local refcounts (lost updates -> leak
    or use-after-free).  With the fiber at the head (attached), a dropper only
    queues and sets the merge bit.

    The race is rare; the ordering is not, so check the ordering on every
    drain: stats() counts drains that ran with the hub's state heading the
    bucket, and that must stay 0 (the old order made it equal to every drain).
    Holds on every interpreter; it only matters on 3.15.0rc3+.
    """
    assert_pass(_CROSS_HUB_DROP_WORKLOAD + r'''
st = stackweave_c.stats()
merges, hub_first = st["brc_release_merges"], st["brc_release_merges_hub_first"]
print("merges=%d hub_first=%d" % (merges, hub_first), flush=True)
assert hub_first == 0, (
    "%d of %d drains ran with the hub's detached state heading the bucket, "
    "where a CPython 3.15.0rc3+ dropper merges on its behalf" % (hub_first, merges))
assert merges > 0, "no parked fiber's queue was drained on its hub's stack"
print("PASS", flush=True)
''', timeout=90)


@TODO_MIGRATION_FAIL(
    'importlib._ModuleLock keys on _thread.get_ident() at Python level; fix is a fiber-aware get_ident behind monkey.patch() (gevent-style)')
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
    res = stackweave.Chan(2)
    def importer(i):
        try:
            m = importlib.import_module("parks_on_import")
            res.send((i, m.READY, None))
        except BaseException as e:
            res.send((i, None, repr(e)))
    stackweave.fiber(importer, 0)
    stackweave.sleep(0)          # first importer is inside the module body now
    stackweave.fiber(importer, 1)
    got = sorted(res.recv()[0] for _ in range(2))
    print("importers: %r moved=%s" % (got, MOVED), flush=True)
    require_migration(MOVED)
    assert got == [(0, True, None), (1, True, None)], got
    print("PASS", flush=True)
stackweave.run(4, main)
''')


@TODO_MIGRATION_FAIL(
    "importlib._ModuleLock is re-entrant per OS thread, so every fiber on the importer's hub re-enters it and gets the half-built module; same fix as the migrating importer above",
    raises=AssertionError)
def test_identity_same_hub_importer_waits_for_a_parked_import():
    """Known gap: importlib's _ModuleLock keys its owner on
    _thread.get_ident(), which every fiber on one hub shares.  While one fiber
    is parked inside a module body, a second fiber on the same hub that
    imports the module re-enters the lock instead of waiting, and gets the
    partially initialised module.  No migration: both importers are spawned
    onto hub 0, which keeps them there, and the first stays inside the body
    until the second has called import.
    """
    rc, out, err = run_scenario(r'''
import tempfile, importlib
_watchdog(20)
d = tempfile.mkdtemp()
with open(os.path.join(d, "parks_in_its_body.py"), "w") as f:
    f.write("import __main__\n__main__.IN_BODY = True\n__main__.GATE.recv()\nLATE = 2\n")
sys.path.insert(0, d)
GATE = stackweave.Chan(0)
IN_BODY = False
def main():
    res = stackweave.Chan(2)
    calling = []
    def importer(tag):
        calling.append(tag)
        try:
            res.send((tag, importlib.import_module("parks_in_its_body").LATE))
        except BaseException as e:
            res.send((tag, repr(e)))
    stackweave_c.mn_fiber(lambda: importer("A"), hub=0)
    while not IN_BODY:
        stackweave.sleep(0.001)
    stackweave_c.mn_fiber(lambda: importer("B"), hub=0)
    while "B" not in calling:
        stackweave.sleep(0.001)
    print("TRIGGER B called import while A was parked in the module body", flush=True)
    # B either returns at once (the gap) or blocks on the module lock; give it
    # a moment either way, then let A finish the import.
    got = {}
    deadline = time.monotonic() + 0.5
    while not got and time.monotonic() < deadline:
        r = res.try_recv()
        if r is not None:
            got[r[0][0]] = r[0][1]
        stackweave.sleep(0.005)
    GATE.send(1)
    while len(got) < 2:
        r, _ = res.recv()
        got[r[0]] = r[1]
    print("RESULT", got, flush=True)
stackweave.run(4, main)
''')
    if "TRIGGER" not in out:
        pytest.fail("the second importer never called import while the first "
                    "was in the module body: rc=%s %s" % (rc, _key_line(out, err)))
    results = [l for l in out.splitlines() if l.startswith("RESULT ")]
    if rc != 0 or not results:
        pytest.fail("rc=%s: %s" % (rc, _key_line(out, err)))
    got = ast.literal_eval(results[0][len("RESULT "):])
    if got.get("A") != 2:
        pytest.fail("the importer parked in the module body failed: %r" % (got,))
    assert got.get("B") == 2, (
        "the second importer on the same hub got the half-built module", got)


@TODO_MIGRATION_FAIL(
    "a timer wake re-queues the sleeper on the hub whose heap held it, ignoring pin_hub1; a channel or park wake routes through the pin",
    raises=AssertionError)
def test_sched_pinned_fiber_resumes_on_its_hub_after_a_sleep():
    """Known gap: G.pin(N) confines a fiber's next resume to hub N, and a
    channel wake honours that, but a sleep -- timed, or sleep(0) -- resumes the
    fiber on the hub it slept on.  The channel wake runs first, as the control
    that the pin itself took.
    """
    rc, out, err = run_scenario(r'''
_watchdog(30)
def main():
    g = stackweave_c.current_g()
    n = stackweave_c.mn_hub_count()
    def pin_next():
        target = (stackweave_c.mn_current_hub() + 1) % n
        g.pin(target)
        return target
    target = pin_next()
    park_n_times(1)
    print("CONTROL", stackweave_c.mn_current_hub() == target, flush=True)
    landed = {}
    for how, sleep in (("sleep(0.02)", lambda: stackweave.sleep(0.02)),
                       ("sleep(0)", lambda: stackweave.sleep(0))):
        target = pin_next()
        sleep()
        landed[how] = (target, stackweave_c.mn_current_hub())
    g.pin(None)
    print("LANDED", landed, flush=True)
stackweave.run(4, main)
''')
    if "CONTROL True" not in out:
        pytest.fail("a channel wake did not land on the pinned hub, so the pin "
                    "itself is broken: rc=%s %s" % (rc, _key_line(out, err)))
    landed = [l for l in out.splitlines() if l.startswith("LANDED ")]
    if rc != 0 or not landed:
        pytest.fail("rc=%s: %s" % (rc, _key_line(out, err)))
    got = ast.literal_eval(landed[0][len("LANDED "):])
    assert all(want == hub for want, hub in got.values()), (
        "(pinned hub, hub it resumed on) after each sleep: %r" % (got,))


def test_identity_current_frames_lists_the_running_fiber_under_get_ident():
    """Was a gap (fixed: a running fiber's tstate carries the hub's thread id, a parked one an id that is no thread's): a fiber's tstate->thread_id is its SPAWNER's thread while
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
stackweave.run(4, main)
''')


def test_sched_pinned_wake_is_prompt_after_pending_drifts():
    """Was a gap (fixed: every idle hub without netpoll/iouring work waits in
    the announced, signalled idle-condvar wait, whatever its own `pending`
    reads): after one unpinned workload the per-hub `pending` counters no
    longer describe what each hub owns (only their SUM is exact), and a hub
    whose own count was <= 0 idled in an uninterruptible runloom_sleep_ns nap
    that neither hub_submit's idle_cond signal nor the run-queue kick could
    reach.  A ping-pong between fibers pinned to hubs 0 and 1 was ~17x slower
    on such a pool than on a fresh one (7.4k vs 130k round-trips/s on an M5).
    Compared against a fresh pool in the same process so a slow runner shifts
    both sides.
    """
    assert_pass(r'''
_watchdog(50)
def pingpong_p50_us(n):
    a, b = stackweave_c.Chan(0), stackweave_c.Chan(0)
    lat = []
    def pinger():
        for i in range(n):
            t0 = time.perf_counter_ns()
            a.send(i)
            b.recv()
            lat.append(time.perf_counter_ns() - t0)
    def ponger():
        for _ in range(n):
            v, _ = a.recv()
            b.send(v)
    stackweave_c.mn_fiber(pinger, hub=0)
    stackweave_c.mn_fiber(ponger, hub=1)
    stackweave_c.mn_run()
    lat.sort()
    return lat[len(lat) // 2] / 1e3
def drift(n=16):
    # Deterministic: spawned on hub 0 (+1 there), re-pinned to hub 1 and woken,
    # so they finish on hub 1 (-1 there) -- what random placement does to some
    # hub after any unpinned workload.
    ch = stackweave_c.Chan(0)
    def mover():
        stackweave_c.current_g().pin(1)
        ch.recv()
    def feeder():
        stackweave_c.sched_sleep(0.01)   # movers park first, or they never move
        for _ in range(n):
            ch.send(1)
    for _ in range(n):
        stackweave_c.mn_fiber(mover, hub=0)
    stackweave_c.mn_fiber(feeder, hub=2)
    stackweave_c.mn_run()
stackweave_c.mn_init(4)
fresh = pingpong_p50_us(2000)
drift()
pending = [h["pending"] for h in stackweave_c.mn_hub_states()]
drifted = pingpong_p50_us(2000)
stackweave_c.mn_fini()
print("round-trip p50: fresh %.1f us, drifted %.1f us, per-hub pending %s"
      % (fresh, drifted, pending), flush=True)
require_migration(pending[1] < 0)
assert drifted < max(50.0, 3 * fresh), (
    "pinned cross-hub round-trip p50 %.0f us on a drifted pool vs %.0f us fresh"
    % (drifted, fresh))
print("PASS", flush=True)
''', timeout=90)


def test_sched_spawn_onto_an_idle_remote_hub_runs_promptly():
    """Was a gap (the same fix as the drifted-pending one above): a hub that
    owns nothing yet -- the target of mn_fiber(fn, hub=N) while it idles --
    took the uninterruptible nap, so the spawn's hub_submit signal missed it
    and the new fiber's first run waited out the 100-500 us nap: ~250 us p50
    against ~5 us for a spawn onto the spawner's own hub (M5).  Compared
    against own-hub spawns in the same process so a slow runner shifts both
    sides; the spawner sleeps between spawns so the target hub is idle again
    each time.
    """
    assert_pass(r'''
_watchdog(50)
def spawn_p50_us(target, n=300, gap_s=0.0003):
    lat = []
    def spawner():
        for _ in range(n):
            t0 = time.perf_counter_ns()
            def first_run(t0=t0):
                lat.append(time.perf_counter_ns() - t0)
            stackweave_c.mn_fiber(first_run, hub=target)
            stackweave_c.sched_sleep(gap_s)
    stackweave_c.mn_fiber(spawner, hub=0)
    stackweave_c.mn_run()
    assert len(lat) == n, (len(lat), n)
    lat.sort()
    return lat[len(lat) // 2] / 1e3
stackweave_c.mn_init(4)
own = spawn_p50_us(0)
remote = spawn_p50_us(1)
stackweave_c.mn_fini()
print("spawn->first-run p50: own hub %.1f us, idle remote hub %.1f us"
      % (own, remote), flush=True)
assert remote < max(100.0, 4 * own), (
    "spawn onto an idle remote hub first ran after %.0f us p50 vs %.0f us on "
    "the spawner's own hub" % (remote, own))
print("PASS", flush=True)
''', timeout=90)


def test_sched_local_wake_is_stolen_promptly_by_a_shallow_idle_hub(monkeypatch):
    """Was a gap (fixed: an idle-condvar wait that is not registered for WAKEP
    is capped at 200 us, STACKWEAVE_IDLE_UNREG_WAIT_US): a fiber pinned to
    hub 0 wakes a receiver -- a local wake, so the receiver lands on hub 0's
    deque -- and then spins 2 ms, so only a steal by the idle hub 1 can run
    the receiver in time.  WAKEP kicks only hubs registered for a wait past
    2 ms; with the idle backoff a hub spends its first ~3 ms of idleness in
    UNREGISTERED waits of 0.4/0.8/1.6 ms that nothing interrupts for stealable
    surplus, so hub 1 was always in one of them when the next wake landed.
    The receiver has no sleeps or timers, so nothing else wakes hub 1.

    What the cap buys depends on how promptly the machine's timers fire, so
    the scenario runs with the cap and with it turned off
    (STACKWEAVE_IDLE_UNREG_WAIT_US=0), twice each, interleaved.  The measure
    is the mean STEAL DELAY: a stolen round counts its wake-to-run latency, a
    round the idle hub never stole within the spin counts the whole spin.  On
    an M5 it is ~10 us capped vs 0.9-1.3 ms uncapped.  The spin is 2 ms, or 4x
    what a 200 us sleep really takes if that is longer: a capped wait has to
    end inside it for the cap to show, and with QoS timer coalescing a 200 us
    sleep takes 1-10 ms (taskpolicy -c utility / -b) and 2.7 ms on the macOS
    CI runner, where a fixed 2 ms spin left capped 0.7-0.8x uncapped and the
    test flaked.  An absolute bound (capped H=2 against an H=4 reference)
    failed on that runner too.  Each run's delay is taken as a share of its
    own window.  The spin stops growing at 40 ms, to bound the run time, so
    it skips where a 200 us sleep takes over 10 ms.  It also skips, instead of
    failing, when the process was starved of CPU (a spinning thread got under
    30% of a core, e.g. utility QoS under default-QoS CPU hogs: 34-40% stolen,
    capped ~ uncapped), and when the timers have a floor (in the median run a
    1600 us sleep took under 3x a 200 us one; 4-8x is usual, even under QoS
    coalescing or CPU hogs), under which capped and uncapped waits are equally
    long.  An unstarved, unfloored run still asserts, so a real regression
    fails wherever the machine can show it.
    """
    monkeypatch.setenv("STACKWEAVE_IDLE_BACKOFF_MS", "32")   # the cap needs the backoff
    runs = {"capped": [], "uncapped": []}
    for _ in range(2):
        for kind in ("capped", "uncapped"):
            if kind == "capped":
                monkeypatch.delenv("STACKWEAVE_IDLE_UNREG_WAIT_US", raising=False)
            else:
                monkeypatch.setenv("STACKWEAVE_IDLE_UNREG_WAIT_US", "0")
            rc, out, err = run_scenario(STEAL_DELAY, timeout=60)
            m = re.search(r"DELAY=([0-9.]+) STOLEN=([0-9.]+) SLEEP200=([0-9.]+) "
                          r"CPU=([0-9.]+) SLEEP1600=([0-9.]+) GAP=([0-9.]+) "
                          r"SPIN=([0-9.]+)", out)
            if rc != 0 or m is None:
                print("--- %s scenario stdout ---\n%s\n--- stderr ---\n%s" % (kind, out, err))
                pytest.fail("%s run rc=%s: %s" % (kind, rc, _key_line(out, err)),
                            pytrace=False)
            runs[kind].append(tuple(float(x) for x in m.groups()))
    # Each run sizes its own window, so each delay is compared as a share of
    # its own: in absolute us, two arms whose windows happened to differ
    # (background QoS: 17-24 ms within one test) could pass with the cap off.
    capped = sum(r[0] / r[6] for r in runs["capped"]) / 2
    uncapped = sum(r[0] / r[6] for r in runs["uncapped"]) / 2
    capped_us = sum(r[0] for r in runs["capped"]) / 2
    uncapped_us = sum(r[0] for r in runs["uncapped"]) / 2
    stolen = min(r[1] for r in runs["capped"])
    sleep200 = max(r[2] for kinds in runs.values() for r in kinds)
    cpu = min(r[3] for kinds in runs.values() for r in kinds)
    sleep1600 = max(r[4] for kinds in runs.values() for r in kinds)
    gap = max(r[5] for kinds in runs.values() for r in kinds)
    spin = max(r[6] for kinds in runs.values() for r in kinds)
    # How much longer a 1600 us sleep takes than a 200 us one: ~8x where timers
    # stretch waits in proportion (fine timers, QoS coalescing), near 1 where a
    # fixed floor or added latency swallows the difference.  The median of the
    # four runs: a floored machine reads low in every run, while one noisy
    # probe (2.5x among 6.4-7.0x on macOS CI) must not skip a regression.
    ratios = sorted(r[4] / r[2] for kinds in runs.values() for r in kinds)
    stretch = (ratios[1] + ratios[2]) / 2
    # Every failure and skip message carries all of this (and each run's
    # numbers): a CI log shows the assertion line, not the captured stdout.
    measured = ("capped %.0f us (%.0f%% of its window, stolen >= %.0f%%), uncapped "
                "%.0f us (%.0f%%), windows up to %.0f us; a 200 us sleep takes %.0f us "
                "and a 1600 us one %.0f us (median %.1fx); a spinning thread gets %.0f%% of a "
                "core, its longest clock gap %.0f us; runs (delay us, stolen, "
                "sleep200 us, cpu, sleep1600 us, gap us, window us) %s"
                % (capped_us, 100 * capped, 100 * stolen, uncapped_us, 100 * uncapped,
                   spin, sleep200, sleep1600, stretch, 100 * cpu, gap,
                   {k: [tuple(round(x, 2) for x in r) for r in v]
                    for k, v in runs.items()}))
    print("mean steal delay: " + measured)
    # The window grows with the timers up to 40 ms (see STEAL_DELAY): past
    # 10 ms per 200 us sleep it no longer covers 4 of them.
    if sleep200 > 10000:
        pytest.skip("timers here are too coarse for the window to cover the "
                    "cap's waits -- %s" % measured)
    would_fail = stolen < 0.5 or capped >= 0.7 * uncapped
    # 0.3, not 0.5: equal-QoS oversubscription keeps the probe near 1 on
    # macOS, but Linux CFS gives a fresh thread ~1/N of a core, so 0.5 would
    # skip a real regression on a 2x-oversubscribed ubuntu runner.
    if would_fail and cpu < 0.3:
        pytest.skip("the process was starved of CPU, so the idle hub could not "
                    "steal in time -- %s" % measured)
    # The window assumes timers stretch every wait alike.  Where a floor makes
    # the capped 200 us waits take as long as the uncapped 0.4-1.6 ms ones, both
    # hubs idle equally long and the cap cannot show, so a failure there is no
    # evidence; an unfloored run still asserts.
    if would_fail and stretch < 3:
        pytest.skip("this machine's timers have a floor (a 1600 us sleep takes a median "
                    "%.1fx a 200 us one), so the cap's short waits cannot end "
                    "sooner -- %s" % (stretch, measured))
    # A FAILED steal is a regression too (a skip would hide it): with stealing
    # broken the sender parks on its next send and hub 0 runs the receiver.
    # 0.5, not 0.9, so a briefly starved hub thread on a loaded runner does not
    # flake it.
    assert stolen >= 0.5, (
        "only %.0f%% of wakes were stolen by the idle hub with the cap -- %s"
        % (100 * stolen, measured))
    assert capped < 0.7 * uncapped, (
        "the cap no longer shortens an idle hub's unregistered waits -- %s" % measured)


STEAL_DELAY = r'''
_watchdog(50)
N, WARM = 150, 20
def sleep_us(length_us):
    """Median wall time of a length_us sleep on a fresh thread: how coarse this
    machine's timers are right now.  A 200 us and a 1600 us sleep that take
    about as long point at a fixed timer floor, under which the cap's short
    waits cannot end sooner than the uncapped ones."""
    out = []
    def probe():
        for _ in range(30):
            t0 = time.perf_counter_ns()
            time.sleep(length_us / 1e6)
            out.append(time.perf_counter_ns() - t0)
    t = threading.Thread(target=probe)
    t.start()
    t.join()
    out.sort()
    return out[len(out) // 2] / 1e3
def cpu_share():
    """The smaller share of a core two threads (one per hub) get while each
    spins 20 ms -- well under 1 when higher-priority load starves this
    process -- and the largest gap between consecutive clock reads in either
    spin.  A gap of several ms with a share near 1 points at the host taking
    the vCPU away, which the guest still charges as this thread's CPU time."""
    out, gaps = [], []
    def probe():
        w0, c0 = time.perf_counter_ns(), time.thread_time_ns()
        last, gap = w0, 0
        while True:
            now = time.perf_counter_ns()
            gap = max(gap, now - last)
            last = now
            if now - w0 >= 20_000_000:
                break
        out.append((time.thread_time_ns() - c0) / (time.perf_counter_ns() - w0))
        gaps.append(gap)
    ts = [threading.Thread(target=probe) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return min(out), max(gaps) / 1e3
def steal_delay_us():
    ch, lat, where = stackweave_c.Chan(0), [], []
    def sender():
        for _ in range(N + WARM):
            ch.send(time.perf_counter_ns())
            end = time.perf_counter_ns() + SPIN_US * 1000
            while time.perf_counter_ns() < end:
                pass
        ch.send(None)
    def receiver():
        while True:
            t0, _ = ch.recv()
            if t0 is None:
                return
            lat.append(time.perf_counter_ns() - t0)
            where.append(stackweave_c.mn_current_hub())
    stackweave_c.mn_init(2)
    stackweave_c.mn_fiber(sender, hub=0)
    stackweave_c.mn_fiber(receiver)
    stackweave_c.mn_run()
    stackweave_c.mn_fini()
    lat, where = lat[WARM:], where[WARM:]
    # A round hub 1 did not steal ran on hub 0 once the sender parked on its
    # next send: the idle hub missed the whole spin, so charge it the spin.
    delay = [l / 1e3 if h != 0 else SPIN_US for l, h in zip(lat, where)]
    return sum(delay) / len(delay), sum(h != 0 for h in where) / len(where)
slept, slept1600 = sleep_us(200), sleep_us(1600)
# The spin is the window a capped wait must end inside for the idle hub to
# steal in time, so it follows how long a 200 us wait really takes here: at a
# fixed 2 ms, a runner whose 200 us waits took 2.7 ms could not let the cap
# show (capped 1.0-1.3 ms vs uncapped 1.5-1.6 ms on macOS CI).  4x keeps the
# uncapped hub inside its unregistered 0.4/0.8/1.6 ms waits (14x a 200 us one
# nominally, ~12x measured on that runner) for the whole window, as on a
# machine with fine timers.  At most 40 ms, so a run stays well inside the
# watchdog; past 10 ms per 200 us sleep the test skips.
SPIN_US = min(max(2000.0, 4 * slept), 40000.0)
cpu, gap = cpu_share()
delay, stolen = steal_delay_us()
print("DELAY=%.1f STOLEN=%.3f SLEEP200=%.0f CPU=%.2f SLEEP1600=%.0f GAP=%.0f "
      "SPIN=%.0f" % (delay, stolen, slept, cpu, slept1600, gap, SPIN_US),
      flush=True)
'''


def test_sched_foreign_thread_wake_reaches_a_shallow_idle_hub_promptly():
    """Was a gap (fixed: a global run-queue push kicks one waiting hub, with a
    Dekker re-check on the hub side): a foreign-thread wake reached a parked
    hub only through wakep_one, which fires once the idle wait exceeds 2 ms;
    at a faster cadence the fiber waited for the next 1 ms idle pump (p99
    380-600 us on a laptop, 4.5-6.8 ms on a 3-core CI runner, against
    11-38 us on the per-hub scheduler).

    Without the kick the median wake waits for the 1 ms idle pump (about
    500 us, and several ms on a loaded runner), so the median is what is
    bounded; the p99 gets only a loose cap, since a slow runner's scheduling
    noise lands there (252 us after the fix on a 3-core runner).
    """
    rc, out, err = run_scenario(WAKE_LATENCY, timeout=90)
    if "NOMIG" in out:
        pytest.skip("no cross-hub migration observed on this machine")
    m = re.search(r"P50=([0-9.]+) P99=([0-9.]+)", out)
    if rc != 0 or m is None:
        print("--- scenario stdout ---\n%s\n--- scenario stderr ---\n%s" % (out, err))
        pytest.fail("rc=%s: %s" % (rc, _key_line(out, err)), pytrace=False)
    p50, p99 = float(m.group(1)), float(m.group(2))
    print("foreign-thread wake latency p50=%.0f us p99=%.0f us" % (p50, p99))
    assert p50 < 250 and p99 < 2000, (
        "foreign-thread wake latency p50=%.0f us p99=%.0f us" % (p50, p99))


WAKE_LATENCY = r'''
_watchdog(60)
N = 1500
def _probe():
    require_migration(force_migrate())
stackweave.run(4, _probe)
def main():
    ch = stackweave.Chan(0)
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
    print("P50=%.1f P99=%.1f" % (p50, p99), flush=True)
stackweave.run(4, main)
'''


# ---------------------------------------------------------------------------
# The signal-wake heap race (PR #23 review, 7.5 #6; TSan finding A1).  A
# raising SIGALRM handler is delivered INTO a parked io-sleeper, which is woken
# from the main thread through the global run-queue and resumes on some other
# hub.  It used to edit its ORIGIN hub's sleep heap from there while that hub
# popped timers; the window is ~100 ns per delivery, so a release build never
# showed a symptom, and only TSan and the sleepheap oracle below catch it.  Each
# hub holds one undelivered exception at a time, so a burst can find every
# slot full and carry the exception out of run(): that ends the stress early
# and is tolerated; a crash, a lost churner or an early wake is not.
# ---------------------------------------------------------------------------

def test_sched_signal_woken_io_sleeper_survives_origin_heap_churn():
    """A sleep the signal wake abandoned never wakes the fiber's next sleep.

    A signal-woken io sleeper that resumed on another hub used to remove
    itself from its ORIGIN hub's sleep heap, racing that hub's timer pop and
    its churners' sleeps (A1 in docs/dev/TSAN.md).  Now the entry stays where
    it is, and the origin hub drops it on its own thread.  So the risk moves
    to the abandoned entry: the fiber may already be sleeping again, on the
    same heap or another, when the old entry comes due.  If the entry could
    claim that next sleep, the sleep would return before its deadline.  Each
    entry therefore carries the sleep's ticket and loses its claim once the
    ticket has moved on.

    The recipients re-sleep the moment a signal lands, so most deliveries
    leave an abandoned entry due within the recipient's next sleep.  Every
    sleep that returns normally is checked against its deadline, and every
    churner must finish: a lost heap entry strands one, a doubled one resumes
    a fiber twice.  main stops the itimer while the recipients are still
    parked and only then lets them go, so no signal is left to escape run()
    once nobody can take it, and main itself counts the finished churners --
    a stranded one would otherwise just hang run() into the watchdog.
    test_sched_only_the_owning_hub_mutates_its_sleep_heap guards the race
    itself.
    """
    assert_pass(r'''
import signal
_watchdog(40)
NS, NR, DUR = 64, 8, 3.0
TOL = 0.0002                    # float rounding between the two clocks' reads
done = bytearray(NS)
early = bytearray(NS + NR)      # one slot per fiber: race-free GIL-off
hits, delivered, finished = [0], [0], [False]
stop = [False]
recipients = stackweave.WaitGroup()
class Tick(Exception): pass
def handler(signum, frame):
    hits[0] += 1
    raise Tick()
signal.signal(signal.SIGALRM, handler)      # main thread, before run()
def churner(i):
    t_end = time.monotonic() + DUR
    while time.monotonic() < t_end:
        t0 = time.monotonic()
        stackweave.sleep(0.001)
        if time.monotonic() - t0 < 0.001 - TOL:
            early[i] = min(early[i] + 1, 255)
    done[i] = 1
def recipient(r):
    try:
        while not stop[0]:
            t0 = time.monotonic()
            try:
                stackweave_c.sched_sleep_io(0.01)
            except Tick:
                delivered[0] += 1
                continue
            if time.monotonic() - t0 < 0.01 - TOL:
                early[NS + r] = min(early[NS + r] + 1, 255)
    finally:
        recipients.done()
def main():
    recipients.add(NR)
    for i in range(NS):
        stackweave.fiber(churner, i)
    for r in range(NR):
        stackweave.fiber(recipient, r)
    stackweave.sleep(0.1)                   # recipients are parked
    signal.setitimer(signal.ITIMER_REAL, 0.001, 0.001)
    stackweave.sleep(DUR)
    # Stop the source while the recipients still take signals: the main
    # thread runs handlers on a ~16 ms poll, so one may still be pending.
    signal.setitimer(signal.ITIMER_REAL, 0, 0)
    stackweave.sleep(0.2)
    stop[0] = True
    recipients.wait()
    t_end = time.monotonic() + 5.0          # churners end DUR after they start
    while sum(done) < NS and time.monotonic() < t_end:
        stackweave.sleep(0.01)
    if sum(done) < NS:                      # run() would never return: say why
        print("LOST %d of %d churners: a sleeper was never woken"
              % (NS - sum(done), NS), flush=True)
        os._exit(4)
    finished[0] = True
try:
    stackweave.run(4, main)
except Tick:
    signal.setitimer(signal.ITIMER_REAL, 0, 0)
print("signals=%d delivered into io-sleepers=%d main finished=%s churners %d/%d, "
      "sleeps cut short: churners %d recipients %d"
      % (hits[0], delivered[0], finished[0], sum(done), NS,
         sum(early[:NS]), sum(early[NS:])), flush=True)
# Handlers run on the main thread's ~16 ms poll; a loaded CI runner starves
# that poll, so the floor is a fraction of the ~180 deliveries an idle box gets.
assert delivered[0] >= 20, "only %d signals reached a parked io-sleeper" % delivered[0]
assert not any(early), "%d sleeps returned before their deadline" % sum(early)
assert finished[0], "a signal escaped run() before main counted the churners"
assert sum(done) == NS, "%d sleepers lost" % (NS - sum(done))
print("PASS", flush=True)
''')


def test_sched_only_the_owning_hub_mutates_its_sleep_heap():
    """Was a gap (fixed: the woken sleeper leaves its entry behind and the origin hub purges it): only a hub's own thread may push, pop or remove on its sleep heap.

    The heap is a plain array with no lock.  A select.poll / no-fd select
    reprobe sleeps in it with sched_sleep_io, and a raised signal handler on
    the main thread wakes that sleeper through the global run-queue, so it can
    resume on any hub.  If the woken fiber then edits its origin hub's heap,
    it races that hub's timer pop and its other fibers' sleeps: plain stores
    that can lose a sleeper or schedule one twice.  TSan reports it (A1 in
    docs/dev/TSAN.md), but a release build almost never shows a symptom, so
    the churn test above passes either way.

    STACKWEAVE_DEBUG=sleepheap turns the race into a deterministic abort on a
    release build: every heap mutation checks that it runs on the owning hub's
    thread.  The scenario needs at least one delivery that resumed on a
    different OS thread from the one it parked on, and skips loudly without.
    """
    assert_pass(r'''
import signal
_watchdog(40)
NS, NR, DUR = 32, 8, 2.0
hits = [0]
delivered = bytearray(NR)       # one slot per recipient: race-free GIL-off
moved = bytearray(NR)
class Tick(Exception): pass
def handler(signum, frame):
    hits[0] += 1
    raise Tick()
signal.signal(signal.SIGALRM, handler)      # main thread, before run()
def churner():
    t_end = time.monotonic() + DUR
    while time.monotonic() < t_end:
        stackweave.sleep(0.001)
def recipient(r):
    t_end = time.monotonic() + DUR
    while time.monotonic() < t_end:
        before = threading.get_ident()
        try:
            stackweave_c.sched_sleep_io(0.01)
        except Tick:
            delivered[r] = min(delivered[r] + 1, 255)
            if threading.get_ident() != before:
                moved[r] = min(moved[r] + 1, 255)
def main():
    for _ in range(NS):
        stackweave.fiber(churner)
    for r in range(NR):
        stackweave.fiber(recipient, r)
    stackweave.sleep(0.1)                   # recipients are parked
    signal.setitimer(signal.ITIMER_REAL, 0.001, 0.001)
    t_end = time.monotonic() + DUR + 0.5
    while time.monotonic() < t_end:
        try:
            stackweave.sleep(t_end - time.monotonic())
        except Tick:
            pass
    signal.setitimer(signal.ITIMER_REAL, 0, 0)
try:
    stackweave.run(4, main)
except Tick:
    signal.setitimer(signal.ITIMER_REAL, 0, 0)
print("signals=%d delivered=%d of them resumed on another hub=%d"
      % (hits[0], sum(delivered), sum(moved)), flush=True)
require_migration(sum(moved) > 0)
print("PASS", flush=True)
''', env={"STACKWEAVE_DEBUG": "sleepheap"})


def test_sched_an_abandoned_sleep_entry_does_not_mask_a_deadlock():
    """The origin hub drops an abandoned sleep entry promptly, not at its deadline.

    A signal-woken io sleeper's entry stays on the heap of the hub it parked
    on (only that hub may edit it).  Until it is gone it is a dead timer that
    still counts: it keeps that hub's sleep_size non-zero, and the deadlock
    census reads a sleeper as work that will wake a fiber.  A no-fd
    select.select(timeout) sleeps the whole timeout in one go, so an abandoned
    entry could mask a real deadlock for that long -- an hour for
    select.select([], [], [], None).  The signal wake therefore posts the
    origin hub's purge mailbox, and that hub drops the entry at its next loop
    top.

    Here a fiber is interrupted out of a one-hour io sleep, and then every
    fiber blocks on a channel nobody sends to.  With deadlock mode "raise",
    run() must raise within a few census periods, not after the hour.
    """
    assert_pass(r'''
import signal
_watchdog(30)
class Tick(Exception): pass
def handler(signum, frame):
    raise Tick()
signal.signal(signal.SIGALRM, handler)      # main thread, before run()
stackweave_c.set_deadlock_mode(2)           # raise
got = []
never = stackweave.Chan(0)
def sleeper():
    try:
        stackweave_c.sched_sleep_io(3600.0)
    except Tick:
        got.append(time.monotonic())
    never.recv()
def main():
    stackweave.fiber(sleeper)
    signal.setitimer(signal.ITIMER_REAL, 0.2)
    never.recv()
t0 = time.monotonic()
try:
    stackweave.run(2, main)
    print("run() returned", flush=True)
except RuntimeError as e:
    print("raised after %.2fs: %s" % (time.monotonic() - t0, e), flush=True)
    assert "deadlock" in str(e), e
    assert got, "the signal never reached the io sleeper"
    lag = time.monotonic() - got[0]
    assert lag < 5.0, "deadlock reported %.1fs after the last wake source went" % lag
    print("PASS", flush=True)
''')


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
