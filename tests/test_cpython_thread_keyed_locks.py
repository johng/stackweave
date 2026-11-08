"""CPython state keyed on the OS thread, used by fibers.

A fiber has its own PyThreadState, but CPython and the C libraries it wraps
key some things on the OS thread: the owner of a _PyRecursiveMutex, of an
_io buffered stream's lock, of a sqlite connection mutex, of a
multiprocessing SemLock; PyThreadState_SetAsyncExc's target; a thread's daemon
flag.  Several fibers share a hub's OS thread, a parked fiber may resume on
another hub, and a preempted fiber stays on its hub's ready ring.  Each test
states the behaviour plain OS threads get; the ones that fail today are strict
known gaps.  Findings, probes and source pointers:
docs/dev/FT_CPYTHON_LOCK_GAPS.md and tools/probes/ft_locks/.

Every scenario runs in a fresh subprocess (a wedge is a clean watchdog exit)
and prints REACHED once its trigger has actually happened: the fiber moved
inside the callback, the holder was preempted holding the lock, and so on.
A scenario that never reached its trigger fails outright; one that reached it
and then went wrong raises AssertionError, which is the only thing the
known-gap markers accept (raises=AssertionError).

Names are ``test_<area>_<invariant>``:

    identity  a lock or check keyed on the OS thread
    preempt   a lock held by a fiber that was preempted
    memory    the biased-refcount merge queue
    sched     scheduling order
    cost      the price of one PyThreadState per fiber
"""
import re
import sys

import pytest

from known_gaps import INTERMITTENT, KNOWN_GAP, MIGRATION_GAP
from test_cross_hub_migration import run_scenario, _key_line


def run_gap(code, timeout=60, env=None):
    """Run PRELUDE + `code`.  Fail outright unless it printed REACHED; raise
    AssertionError unless it also exited 0 having printed PASS."""
    rc, out, err = run_scenario(code, timeout=timeout, env=env)
    transcript = ("--- scenario stdout ---\n%s\n--- scenario stderr ---\n%s"
                  % (out, err[-6000:]))
    if "REACHED" not in out:
        print(transcript)
        pytest.fail("the scenario never reached its trigger: rc=%s: %s"
                    % (rc, _key_line(out, err)), pytrace=False)
    if rc != 0 or "PASS" not in out:
        print(transcript)
        raise AssertionError("rc=%s: %s" % (rc, _key_line(out, err)))
    return out


# ---------------------------------------------------------------------------
# identity: a lock or check keyed on the OS thread
# ---------------------------------------------------------------------------

@MIGRATION_GAP(
    "sqlite3_step holds the connection mutex (serialized mode) across a user "
    "function; a function that parks and resumes on another hub makes sqlite "
    "release it from the wrong OS thread (macOS: os_unfair_lock abort)",
    raises=AssertionError)
def test_identity_sqlite_callback_survives_a_migration():
    """A sqlite user function that parks and resumes on another hub returns
    its value, and the connection stays usable from every hub.  Run in
    serialized mode (the Debian/Ubuntu/Homebrew default; macOS's system
    library is multi-thread, so the scenario switches it before import)."""
    run_gap(r'''
import ctypes, importlib.util, subprocess
_watchdog(40)
def _linked_sqlite():
    origin = importlib.util.find_spec("_sqlite3").origin
    tool = ["otool", "-L"] if sys.platform == "darwin" else ["ldd"]
    for line in subprocess.check_output(tool + [origin], text=True).splitlines():
        if "libsqlite3" not in line:
            continue
        path = line.split("=>")[-1].split("(")[0].strip()
        return ctypes.CDLL(path)
    return None
lib = _linked_sqlite()
mode = lib.sqlite3_threadsafe() if lib is not None else -1
if mode == 2:                                   # multi-thread: make it serialized
    serialized = lib.sqlite3_config(3) == 0     # SQLITE_CONFIG_SERIALIZED
else:
    serialized = (mode == 1)
print("sqlite threadsafe=%d serialized=%s" % (mode, serialized), flush=True)
import sqlite3
H = 4
def main():
    if not serialized:
        print("could not put sqlite in serialized mode", flush=True)
        return
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.execute("create table t(x)")
    con.executemany("insert into t values (?)", [(i,) for i in range(5)])
    def udf(x):
        if x == 2:
            before = threading.get_ident()
            force_migrate(rounds=20)
            if threading.get_ident() != before:
                print("REACHED: the function resumed on another hub inside sqlite3_step", flush=True)
        return x
    con.create_function("f", 1, udf)
    total = con.execute("select sum(f(x)) from t").fetchone()[0]
    out = {}
    wg = stackweave.WaitGroup(); wg.add(H)
    def use(h):
        out[h] = con.execute("select count(*) from t").fetchone()[0]
        wg.done()
    for h in range(H):
        stackweave_c.mn_fiber(lambda h=h: use(h), hub=h)
    wg.wait()
    print("sum=%s per-hub=%s" % (total, out), flush=True)
    assert total == 10 and out == {h: 5 for h in range(H)}
    print("PASS", flush=True)
stackweave.run(H, main)
''')


@MIGRATION_GAP(
    "C do_warn holds the interpreter's warnings _PyRecursiveMutex (owner: the "
    "OS thread) across showwarning; one that parks and migrates cannot "
    "release it, so every later warn on another hub blocks its hub forever",
    raises=AssertionError)
def test_identity_warnings_lock_released_after_a_migrating_showwarning():
    """A showwarning hook that does I/O (logging.captureWarnings with a
    network handler, say) parks and resumes on another hub.  Afterwards a
    warning on every hub still returns."""
    run_gap(r'''
import warnings
_watchdog(20)
H = 4
warnings.simplefilter("always")
state = {"first": True}
def show(*a, **k):
    if state["first"] and stackweave_c.current_g() is not None:
        state["first"] = False
        before = threading.get_ident()
        force_migrate(rounds=10)
        if threading.get_ident() != before:
            print("REACHED: showwarning resumed on another hub", flush=True)
warnings.showwarning = show
def main():
    warnings.warn("first", UserWarning)
    done = []
    wg = stackweave.WaitGroup(); wg.add(H)
    def other(h):
        warnings.warn("on hub %d" % h, DeprecationWarning)
        done.append(h); wg.done()
    for h in range(H):
        stackweave_c.mn_fiber(lambda h=h: other(h), hub=h)
    wg.wait()
    print("warned on hubs", sorted(done), flush=True)
    print("PASS", flush=True)
stackweave.run(H, main)
''')


@MIGRATION_GAP(
    "importlib runs every finder.find_spec() under the global import lock "
    "(_imp, a _PyRecursiveMutex owned by the OS thread); a finder that parks "
    "and migrates gets 'not holding the import lock' and leaks the lock, so "
    "every later import hangs",
    raises=AssertionError)
def test_identity_import_lock_released_after_a_migrating_finder():
    """A meta-path finder that does I/O (setuptools' editable finder calls
    Path.exists) parks and resumes on another hub.  The import fails with
    ModuleNotFoundError, and imports on every hub still work."""
    run_gap(r'''
import importlib, importlib.abc
_watchdog(20)
H = 4
class ParkingFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == "nosuch_mod_for_the_finder" and stackweave_c.current_g() is not None:
            before = threading.get_ident()
            force_migrate(rounds=5)
            if threading.get_ident() != before:
                print("REACHED: find_spec resumed on another hub", flush=True)
        return None
sys.meta_path.insert(0, ParkingFinder())
def main():
    try:
        import nosuch_mod_for_the_finder  # noqa: F401
        first = "imported?!"
    except ModuleNotFoundError:
        first = "ModuleNotFoundError"
    except Exception as e:
        first = repr(e)
    print("first import:", first, flush=True)
    done = []
    wg = stackweave.WaitGroup(); wg.add(H)
    def other(h):
        importlib.import_module(["colorsys", "fractions", "statistics", "difflib"][h])
        done.append(h); wg.done()
    for h in range(H):
        stackweave_c.mn_fiber(lambda h=h: other(h), hub=h)
    wg.wait()
    print("imported on hubs", sorted(done), flush=True)
    assert first == "ModuleNotFoundError", first
    print("PASS", flush=True)
stackweave.run(H, main)
''')


@pytest.mark.parametrize("patched", [False, True], ids=["stock", "monkey"])
@MIGRATION_GAP(
    "a recursive multiprocessing SemLock (mp.Value's lock) decides 'mine' by "
    "the OS thread (semaphore.c ISMINE); after a migration the holder's "
    "re-acquire waits on itself in sem_wait, with or without monkey.patch",
    raises=AssertionError)
def test_identity_mp_value_lock_reenters_after_a_migration(patched):
    """`with v.get_lock(): <park, migrate>; v.value += 1` -- the property
    re-acquires the same recursive lock, which must still count as held."""
    run_gap(r'''
_watchdog(25)
if PATCHED:
    import stackweave.monkey as monkey; monkey.patch()
import multiprocessing as mp
def main():
    v = mp.Value("i", 0)
    with v.get_lock():
        before = threading.get_ident()
        force_migrate(rounds=20)
        if threading.get_ident() != before:
            print("REACHED: moved to another hub holding the lock", flush=True)
        v.value += 1
    assert v.get_lock().acquire(timeout=2)
    v.get_lock().release()
    print("value", v.value, flush=True)
    print("PASS", flush=True)
stackweave.run(2, main)
'''.replace("PATCHED", repr(patched)))


@pytest.mark.parametrize("raw", ["socket-park", "python-raw-preempt"])
@KNOWN_GAP(
    "_io.Buffered* keeps its lock's owner as the OS thread "
    "(_enter_buffered_busy); a fiber on the same hub as a holder parked or "
    "preempted inside the raw stream gets 'reentrant call inside' instead of "
    "waiting",
    raises=AssertionError)
def test_identity_shared_buffered_stream_waits_on_one_hub(raw):
    """Two fibers on one hub use one buffered stream; the first is inside the
    raw stream's Python code (a cooperative recv that parks, or a long
    write that is preempted).  The second must wait, as a thread would."""
    run_gap(r'''
import io, time as _t
_watchdog(20)
RAW = "RAWKIND"
if RAW == "socket-park":
    import stackweave.monkey as monkey; monkey.patch()
import socket
inside = []
def _step(end):
    return _t.perf_counter() < end
class SpinRaw(io.RawIOBase):
    def writable(self): return True
    def write(self, b):
        inside.append(1)
        end = _t.perf_counter() + 0.3
        while _step(end):
            pass
        inside.pop()
        return len(b)
def main():
    if RAW == "socket-park":
        a, b = socket.socketpair()
        f = a.makefile("rb")
        op = lambda: f.read(4)
    else:
        f = io.BufferedWriter(SpinRaw(), buffer_size=4)
        op = lambda: (f.write(b"x" * 8), f.flush())
    res = {}
    wg = stackweave.WaitGroup(); wg.add(2)
    def worker(i):
        if i == 1 and (RAW == "socket-park" and 0 not in res or inside):
            print("REACHED: second fiber starts while the first is inside the raw stream", flush=True)
        try:
            op(); res[i] = "ok"
        except Exception as e:
            res[i] = repr(e)
        wg.done()
    stackweave_c.mn_fiber(lambda: worker(0), hub=0)
    stackweave.sleep(0.05)
    stackweave_c.mn_fiber(lambda: worker(1), hub=0)
    stackweave.sleep(0.05)
    if RAW == "socket-park":
        b.sendall(b"onetwo!!")
    wg.wait()
    print(res, flush=True)
    assert res == {0: "ok", 1: "ok"}, res
    print("PASS", flush=True)
stackweave.run(2, main)
'''.replace("RAWKIND", raw))


@KNOWN_GAP(
    "PyThreadState_SetAsyncExc(ident) picks the newest tstate whose thread_id "
    "is that OS thread; once the target fiber has parked, that is whatever "
    "else runs on its hub",
    raises=AssertionError)
def test_identity_async_exc_reaches_the_fiber_that_owns_the_ident():
    """The stopit / func_timeout idiom: a watchdog thread records the target's
    threading.get_ident() and later posts an exception to it.  The target
    gets it; a bystander on the same hub does not."""
    run_gap(r'''
import ctypes, time as _t
_watchdog(20)
class Timeout(Exception):
    pass
def main():
    got = {}
    parked = []
    wg = stackweave.WaitGroup(); wg.add(2)
    def target():
        ident = threading.get_ident()
        def fire():
            _t.sleep(0.1)
            was_parked = bool(parked)
            n = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(ident), ctypes.py_object(Timeout))
            if n == 1 and was_parked:
                print("REACHED: exception posted while the target was parked", flush=True)
        threading.Thread(target=fire, daemon=True).start()
        try:
            parked.append(1)
            stackweave.sleep(0.3)
            parked.pop()
            for _ in range(200000):
                pass
            got["target"] = "not interrupted"
        except Timeout:
            got["target"] = "interrupted"
        wg.done()
    def bystander():
        try:
            end = _t.perf_counter() + 0.5
            while _t.perf_counter() < end:
                [0] * 10
            got["bystander"] = "ok"
        except Timeout:
            got["bystander"] = "got the target's exception"
        wg.done()
    stackweave_c.mn_fiber(target, hub=0)
    stackweave_c.mn_fiber(bystander, hub=0)
    wg.wait()
    print(got, flush=True)
    assert got == {"target": "interrupted", "bystander": "ok"}, got
    print("PASS", flush=True)
stackweave.run(2, main)
''')


@MIGRATION_GAP(
    "asyncio's debug-mode _check_thread compares threading.get_ident() with "
    "the loop's thread; a stock loop run in an M:N fiber parks in its patched "
    "selector, resumes on another hub, and call_soon raises",
    raises=AssertionError)
def test_identity_asyncio_debug_loop_keeps_working_after_a_migration():
    """asyncio.run(debug=True) (as -X dev and IsolatedAsyncioTestCase use)
    inside a fiber whose loop moves between hubs keeps scheduling callbacks."""
    run_gap(r'''
import stackweave.monkey as monkey; monkey.patch()
import asyncio, logging
logging.getLogger("asyncio").setLevel(logging.CRITICAL)   # slow-callback noise
_watchdog(20)
def main():
    res = {}
    def on_error(loop, context):
        # The check raises inside a loop callback: the loop reports it here
        # and the callback is lost (the loop then waits forever).
        exc = context.get("exception")
        if isinstance(exc, RuntimeError) and "other than the current one" in str(exc):
            print("REACHED: the debug thread check fired after the loop moved hubs", flush=True)
        res["error"] = repr(exc)
    def runner():
        async def co():
            asyncio.get_running_loop().set_exception_handler(on_error)
            seen = set()
            for _ in range(300):
                await asyncio.sleep(0.001)
                seen.add(threading.get_ident())
                asyncio.get_running_loop().call_soon(lambda: None)
            return len(seen)
        try:
            res["os_threads"] = asyncio.run(co(), debug=True)
        except RuntimeError as e:
            res["error"] = str(e)
    done = stackweave.Chan(1)
    stackweave.fiber(lambda: (runner(), done.send(1)))
    done.recv()
    print(res, flush=True)
    if res.get("os_threads", 0) > 1 or "other than the current one" in res.get("error", ""):
        print("REACHED: the loop's fiber ran on more than one hub", flush=True)
    assert "error" not in res, res["error"]
    print("PASS", flush=True)
stackweave.run(4, main)
''')


@KNOWN_GAP(
    "Thread.__init__ copies daemon from current_thread(), which in an M:N "
    "fiber is the hub's daemon _DummyThread, so exit does not join it",
    raises=AssertionError)
def test_identity_thread_started_in_a_fiber_is_joined_at_exit():
    """A threading.Thread started from a fiber is non-daemon, as one started
    from the main thread is, so the interpreter waits for it at exit."""
    run_gap(r'''
import time as _t
def main():
    def work():
        _t.sleep(0.3)
        print("PASS", flush=True)
    t = threading.Thread(target=work)
    t.start()
    print("REACHED: thread started from a fiber, daemon=%s" % t.daemon, flush=True)
stackweave.run(2, main)
''')


@KNOWN_GAP(
    "monkey.patch's os.read/os.write set O_NONBLOCK on a pollable fd and never "
    "clear it; the flag lives on the open file description, which dups, "
    "std streams and child processes share",
    raises=AssertionError)
def test_identity_patched_os_read_leaves_the_fd_mode_alone():
    """A cooperative os.read from a fiber leaves the fd blocking, as it found
    it, for whatever else shares the file description."""
    run_gap(r'''
import stackweave.monkey as monkey; monkey.patch()
def main():
    r, w = os.pipe()
    os.write(w, b"x")
    assert os.get_blocking(r)
    assert os.read(r, 1) == b"x"
    print("REACHED: patched os.read ran in a fiber", flush=True)
    print("blocking after:", os.get_blocking(r), flush=True)
    assert os.get_blocking(r)
    print("PASS", flush=True)
stackweave.run(2, main)
''')


@KNOWN_GAP(
    "_PyRuntime.main_tstate is never updated after fork, and every M:N fork "
    "comes from a hub thread, so the child's pending-signal bit lands on a "
    "dead state and handlers run only at the next EINTR (upstream: any "
    "fork from a non-main thread)",
    raises=AssertionError)
def test_identity_child_forked_from_a_fiber_runs_signal_handlers():
    """A child forked from a fiber runs a Python signal handler for an
    asynchronous signal (setitimer) while it executes bytecode."""
    run_gap(r'''
import signal, time as _t, warnings
warnings.simplefilter("ignore", DeprecationWarning)   # fork with threads
def child():
    hits = []
    signal.signal(signal.SIGALRM, lambda *a: hits.append(_t.perf_counter()))
    t0 = _t.perf_counter()
    signal.setitimer(signal.ITIMER_REAL, 0.05)
    os.write(1, b"REACHED: child armed an async SIGALRM\n")
    while _t.perf_counter() < t0 + 1.0 and not hits:
        pass
    # Decide before any syscall: os.write checks pending signals itself, which
    # would run a handler the bytecode loop never ran.
    ran_in_loop = bool(hits)
    os.write(1, ("child handler ran during the loop: %s\n" % ran_in_loop).encode())
    os._exit(0 if ran_in_loop else 1)
def main():
    pid = os.fork()
    if pid == 0:
        child()
    _, st = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(st) == 0, "the handler never ran during the loop"
    print("PASS", flush=True)
stackweave.run(2, main)
''')


# ---------------------------------------------------------------------------
# preempt: a lock held by a fiber that was preempted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lock", ["threading.Lock", "_strptime._cache_lock"])
@KNOWN_GAP(
    "a preempted fiber waits on its hub's ready ring, which nothing steals; a "
    "fiber on that hub contending for a stock lock it holds blocks the hub's "
    "OS thread, so the holder never runs again (monkey.patch avoids it for "
    "locks made after patch(); the parking-lot hook would cover _thread locks)",
    raises=AssertionError)
def test_preempt_lock_holder_runs_while_a_same_hub_fiber_waits(lock):
    """Fiber A runs CPU-bound Python inside a stock lock and is preempted; B
    on the same hub then wants the lock.  A finishes and B gets it, as with
    threads.  `_strptime._cache_lock` is taken implicitly by time.strptime."""
    run_gap(r'''
import time as _t, _strptime
_watchdog(15)
LOCK = "LOCKNAME"
lock = threading.Lock() if LOCK == "threading.Lock" else _strptime._cache_lock
def step(end):
    return _t.perf_counter() < end
def main():
    out = {}
    wg = stackweave.WaitGroup(); wg.add(2)
    def A():
        with lock:
            end = _t.perf_counter() + 0.3
            while step(end):
                pass
        out["A"] = "done"; wg.done()
    def B():
        if lock.locked():
            print("REACHED: B runs on the hub while preempted A holds the lock", flush=True)
        t0 = _t.perf_counter()
        if LOCK == "threading.Lock":
            with lock:
                pass
        else:
            _t.strptime("2026-11-08", "%Y-%m-%d")
        out["B_waited"] = round(_t.perf_counter() - t0, 3); wg.done()
    stackweave_c.mn_fiber(A, hub=0)
    stackweave.sleep(0.02)
    stackweave_c.mn_fiber(B, hub=0)
    wg.wait()
    print(out, flush=True)
    print("PASS", flush=True)
stackweave.run(2, main)
'''.replace("LOCKNAME", lock))


@KNOWN_GAP(
    "sqlite's busy handler sleeps inside sqlite3_step on the hub's OS thread, "
    "so a fiber holding the write lock across a park cannot run (its wake "
    "timer is on that hub) until the waiter's timeout expires",
    raises=AssertionError)
def test_preempt_sqlite_writer_waits_for_a_same_hub_transaction():
    """One connection per fiber on one database file.  A holds the write lock
    across a short park; B on the same hub writes.  B waits for A's commit
    (~50 ms) and succeeds, as a thread would."""
    run_gap(r'''
import sqlite3, tempfile, time as _t
_watchdog(30)
path = tempfile.mktemp(suffix=".db")
c0 = sqlite3.connect(path); c0.execute("create table t(x)"); c0.commit(); c0.close()
def main():
    out = {}
    holding = []
    wg = stackweave.WaitGroup(); wg.add(2)
    def holder():
        a = sqlite3.connect(path, timeout=2)
        a.execute("insert into t values (1)")     # implicit BEGIN: write lock
        holding.append(1)
        stackweave.sleep(0.05)
        a.commit(); holding.pop(); a.close(); wg.done()
    def writer():
        if holding:
            print("REACHED: B writes while A holds the write lock", flush=True)
        b = sqlite3.connect(path, timeout=2)
        t0 = _t.monotonic()
        try:
            b.execute("insert into t values (2)"); b.commit(); out["B"] = "ok"
        except Exception as e:
            out["B"] = repr(e)
        out["B_waited"] = round(_t.monotonic() - t0, 3); b.close(); wg.done()
    stackweave_c.mn_fiber(holder, hub=0)
    stackweave.sleep(0.01)
    stackweave_c.mn_fiber(writer, hub=0)
    wg.wait()
    print(out, flush=True)
    assert out["B"] == "ok" and out["B_waited"] < 1.0, out
    print("PASS", flush=True)
stackweave.run(2, main)
''')


# ---------------------------------------------------------------------------
# memory: the biased-refcount merge queue
# ---------------------------------------------------------------------------

def test_memory_queued_objects_merge_on_their_owner_thread():
    """Objects allocated on one hub and dropped on another thread are queued
    to the allocating hub; their merge (and so their dealloc, here) must run
    on that hub's OS thread even when an earlier object's __del__ parks and
    the draining fiber moves.  gc is off, so nothing else merges them.

    The trigger is made deterministic: a plain OS thread drops B, B, B, A
    while the producer fiber spins in C on its hub until the dropper sets a
    flag.  Spinning in C runs no bytecode, so there is no eval-breaker check
    (no merge yet), and keeps the producer's state ATTACHED and at the head
    of the hub's bucket, so every drop queues to it.  (Blocking would not do:
    a fiber's contended lock acquire parks it, and drops made while it is
    parked go to the hub's own state; os.read detaches it, and from CPython
    3.15.0rc3 (gh-157838) a dropper that finds its owner detached merges the
    queue itself, on the dropper's thread.)  The producer's next eval-breaker
    check then merges all four in one batch, A on top (LIFO).  A's __del__
    parks at once (no Python in between that could run a nested merge and
    empty the stack first) and is resumed on another hub by the pin.  The
    B's are still on the fiber's local_objects_to_merge at the park:
    runloom_iframe_brc_release drains them there, on their owner
    (stats()["brc_release_local_drains"]).  Before that fix the fiber carried
    them to the other hub and merged all of them there.

    B records its owner in __init__ and checks it at the first line of
    __del__; nothing else is queued meanwhile, so no nested merge can move
    the fiber between a merge and that check (which is what made the earlier,
    racier version of this scenario count B's that were merged on their
    owner).  A merge on the dropper's own thread is CPython's legitimate
    merge-on-behalf, not this bug, so it is counted apart; here it means the
    setup failed (the producer was detached), and so does an A whose __del__
    never parked: nothing receives the dropper's wake, the send gives up, and
    the scenario fails instead of hanging."""
    run_gap(r'''
import collections, functools, gc, itertools, operator, queue
gc.disable()
_watchdog(50)
ROUNDS, SPIN = 100, 1_000_000
wake = stackweave.Chan(0)
stats = {"a_moved": 0, "a_ahead": 0, "b_del": 0, "b_off_owner": 0,
         "b_on_dropper": 0, "round_b0": 0, "lost": 0, "dropper": None}
dropped = bytearray(1)                      # set by the dropper per round
poll = functools.partial(operator.getitem, dropped, 0)
class A:
    __slots__ = ()
    def __del__(self):
        g = stackweave_c.current_g()
        if g is None:
            return
        if stats["b_del"] == stats["round_b0"]:
            stats["a_ahead"] += 1           # this round's B's still queued
        before = threading.get_ident()
        g.pin((stackweave_c.mn_current_hub() + 1) % stackweave_c.mn_hub_count())
        try:
            wake.recv()                     # park now; resume on another hub
        finally:
            g.pin(None)
        if threading.get_ident() != before:
            stats["a_moved"] += 1
class B:
    __slots__ = ("owner",)
    def __init__(self): self.owner = threading.get_ident()
    def __del__(self):
        stats["b_del"] += 1
        me = threading.get_ident()
        if me == stats["dropper"]:
            stats["b_on_dropper"] += 1      # merged on the dropper's behalf
        elif me != self.owner:
            stats["b_off_owner"] += 1
def send_or_give_up(ch, value, secs):
    """foreign_send with a deadline: False if no fiber came to receive."""
    end = time.monotonic() + secs
    while True:
        try:
            ch.send(value)
            return True
        except RuntimeError:
            if time.monotonic() > end:
                return False
            time.sleep(0.0005)
def main():
    inbox = queue.SimpleQueue()
    def dropper():                          # a plain OS thread
        stats["dropper"] = threading.get_ident()
        while True:
            v = inbox.get()
            if v is None:
                return
            while v:
                v.pop(0)                    # drop B, B, B, then A
            del v
            dropped[0] = 1                  # the producer may go on
            if not send_or_give_up(wake, 1, 5):   # resume A's __del__
                stats["lost"] += 1
    t = threading.Thread(target=dropper, daemon=True)
    t.start()
    wg = stackweave.WaitGroup(); wg.add(1)
    def producer():
        for _ in range(ROUNDS):
            if stats["lost"]:
                break
            stats["round_b0"] = stats["b_del"]
            dropped[0] = 0
            inbox.put([B(), B(), B(), A()])
            while not dropped[0]:           # spin in C, attached, no bytecode
                collections.deque(itertools.islice(iter(poll, 1), SPIN), maxlen=0)
            # the next eval-breaker check merges the drops in one batch, A first
        inbox.put(None)
        wg.done()
    stackweave_c.mn_fiber(producer, hub=0)
    wg.wait()
    t.join()
    stackweave.sleep(0.2)
    print(stats, flush=True)
    if stats["a_moved"] and stats["a_ahead"]:
        print("REACHED: a __del__ moved hubs during a merge drain, with "
              "objects still queued behind it", flush=True)
    assert stats["b_off_owner"] == 0, "%d objects merged off their owner thread" % stats["b_off_owner"]
    assert stats["b_on_dropper"] == 0 and stats["lost"] == 0, (
        "setup failed: the dropper merged on the producer's behalf, or an A's "
        "__del__ ran outside the producer's merge: %r" % stats)
    assert stats["b_del"] == 3 * ROUNDS, stats
    drains = stackweave_c.stats()["brc_release_local_drains"]
    assert drains > 0, "the release never found a part-drained local stack"
    print("local drains:", drains, flush=True)
    print("PASS", flush=True)
stackweave.run(4, main)
''', timeout=90)


_FINALIZER_GAP = (
    "objects dropped on another hub are mostly destroyed by the owning hub's "
    "merge on the hub's own thread state (runloom_iframe_service_merge_queue), "
    "outside any fiber, where a channel op raises 'no goroutine is running'")


# On 3.15 the dropper often merges on behalf of a detached owner
# (gh-157838), so most finalizers run in the dropping fiber and the scenario
# passes on most runs, not all: not strict there.
@(KNOWN_GAP(_FINALIZER_GAP, raises=AssertionError)
  if sys.version_info < (3, 15) else
  INTERMITTENT(_FINALIZER_GAP + " (3.15: usually merged by the dropper)",
               raises=AssertionError))
def test_memory_finalizer_can_use_a_channel_whichever_hub_drops_it():
    """A __del__ that does a cooperative operation (a channel round-trip)
    works no matter which hub dropped the last reference."""
    run_gap(r'''
_watchdog(40)
N = 600
stats = {"del": 0, "err": 0, "errs": set()}
class Obj:
    __slots__ = ("payload",)
    def __init__(self): self.payload = [1, 2, 3]
    def __del__(self):
        stats["del"] += 1
        try:
            c = stackweave.Chan(0)
            stackweave.fiber(lambda: c.send(1))
            c.recv()
        except Exception as e:
            stats["err"] += 1; stats["errs"].add(repr(e)[:90])
def main():
    ch = stackweave.Chan(64)
    wg = stackweave.WaitGroup(); wg.add(2)
    def producer():
        for _ in range(N):
            ch.send(Obj())
        ch.send(None); wg.done()
    def consumer():
        held = []
        while True:
            v, ok = ch.recv()
            if v is None:
                break
            held.append(v)
            if len(held) > 50:
                held.clear()
        held.clear(); wg.done()
    stackweave_c.mn_fiber(producer, hub=0)
    stackweave_c.mn_fiber(consumer, hub=1)
    wg.wait()
    stackweave.sleep(0.3)
    print(stats, flush=True)
    if stats["del"]:
        print("REACHED: finalizers of cross-hub drops ran", flush=True)
    assert stats["err"] == 0, stats["errs"]
    print("PASS", flush=True)
stackweave.run(4, main)
''')


# ---------------------------------------------------------------------------
# sched / cost
# ---------------------------------------------------------------------------

@KNOWN_GAP(
    "a fiber that pins itself onto a hub waits on the global run-queue, which "
    "that hub pulls only every starve_bound (64) ready-ring turns while a "
    "preempted CPU-bound fiber keeps the ring busy: ~64 x 50 ms",
    raises=AssertionError)
def test_sched_fiber_pinned_onto_a_busy_hub_gets_a_turn_promptly():
    """A fiber does `g.pin(0); sleep(0)` while hub 0 runs a CPU-bound fiber;
    it first runs on hub 0 within a few time slices (well under 1 s)."""
    run_gap(r'''
import time as _t
_watchdog(20)
def step(end):
    return _t.perf_counter() < end
def main():
    out = {}
    hog_running = []
    wg = stackweave.WaitGroup(); wg.add(2)
    def hog():
        hog_running.append(1)
        end = _t.perf_counter() + 4.0
        while step(end):
            pass
        hog_running.pop(); wg.done()
    def pinned():
        t0 = _t.perf_counter()
        stackweave_c.current_g().pin(0)
        stackweave.sleep(0)
        out["waited"] = round(_t.perf_counter() - t0, 3)
        out["hub"] = stackweave_c.mn_current_hub()
        if out["hub"] == 0 and (hog_running or out["waited"] > 0.5):
            print("REACHED: pinned onto hub 0 while the hog ran", flush=True)
        wg.done()
    stackweave_c.mn_fiber(hog, hub=0)
    stackweave.sleep(0.2)
    stackweave_c.mn_fiber(pinned, hub=1)
    wg.wait()
    print(out, flush=True)
    assert out["waited"] < 1.0, out
    print("PASS", flush=True)
stackweave.run(2, main)
''')


_TLBC_SCENARIO = r'''
import gc, resource
N = 1000
src = "def big(x):\n" + "".join(
    "    x = (x * %d + %d) %% 1000003\n" % (i, i) for i in range(3000)) + "    return x\n"
ns = {}; exec(src, ns); big = ns["big"]
def peak_mb():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1 << 20) if sys.platform == "darwin" else r / 1024.0
def main():
    gate = stackweave.Chan(0)
    wg = stackweave.WaitGroup(); wg.add(N)
    def f():
        big(1); big(2)
        wg.done()
        gate.recv()
    base = peak_mb()
    for _ in range(N):
        stackweave.fiber(f)
    wg.wait()
    print("REACHED: %d live fibers ran big()" % N, flush=True)
    print("GROWTH_MB %.1f" % (peak_mb() - base), flush=True)
    gate.close()
    print("PASS", flush=True)
stackweave.run(4, main)
'''


@KNOWN_GAP(
    "every per-fiber tstate reserves its own thread-local-bytecode index, so "
    "each live fiber gets its own copy of every function it runs (~195 KB "
    "per fiber for a 144 KB function on macOS), kept after the fibers exit",
    raises=AssertionError)
def test_cost_live_fibers_share_bytecode_within_1_5x_of_tlbc_off():
    """1000 live fibers that ran one large function: memory growth with
    thread-local bytecode on stays within 1.5x of PYTHON_TLBC=0."""
    def growth(env):
        out = run_gap(_TLBC_SCENARIO, env=env, timeout=120)
        return float(re.search(r"GROWTH_MB ([\d.]+)", out).group(1))
    on = growth(None)
    off = growth({"PYTHON_TLBC": "0"})
    print("growth: TLBC on %.1f MB, off %.1f MB" % (on, off))
    assert on <= 1.5 * max(off, 1.0), "TLBC on %.1f MB vs off %.1f MB" % (on, off)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
