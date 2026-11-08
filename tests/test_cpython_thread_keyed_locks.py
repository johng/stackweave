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

def test_identity_sqlite_callback_keeps_its_os_thread_across_a_park():
    """Was a gap (fixed: a fiber is held on its hub while a foreign C caller
    is on its stack): sqlite3_step holds the connection mutex (serialized
    mode) across a user function, and one that parked and resumed on another
    hub made sqlite release it from the wrong OS thread (macOS: an
    os_unfair_lock abort).  A user function that parks on wakes that move a
    plain fiber -- foreign-thread wakes, then a pin to another hub -- must
    resume on the OS thread it entered on and return its value, and the
    connection must stay usable from every hub.  Run in serialized mode (the
    Debian/Ubuntu/Homebrew default; macOS's system library is multi-thread,
    so the scenario switches it before import)."""
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
    # The premise: parked this way outside a callback, this fiber moves.
    require_migration(force_migrate())
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.execute("create table t(x)")
    con.executemany("insert into t values (?)", [(i,) for i in range(5)])
    seen = {}
    def udf(x):
        if x == 2:
            before = threading.get_ident()
            moved = force_migrate(rounds=20)    # ends with a pin to another hub
            print("REACHED: the function parked inside sqlite3_step on wakes "
                  "that move a plain fiber", flush=True)
            seen["moved"] = moved or threading.get_ident() != before
        return x
    con.create_function("f", 1, udf)
    holds = stackweave_c.stats()["mn_c_caller_holds"]
    total = con.execute("select sum(f(x)) from t").fetchone()[0]
    holds = stackweave_c.stats()["mn_c_caller_holds"] - holds
    out = {}
    wg = stackweave.WaitGroup(); wg.add(H)
    def use(h):
        out[h] = con.execute("select count(*) from t").fetchone()[0]
        wg.done()
    for h in range(H):
        stackweave_c.mn_fiber(lambda h=h: use(h), hub=h)
    wg.wait()
    print("sum=%s per-hub=%s moved=%s holds=%d" % (total, out, seen, holds), flush=True)
    assert seen == {"moved": False}, seen
    assert holds > 0, "no park was held on its hub (stats mn_c_caller_holds)"
    assert total == 10 and out == {h: 5 for h in range(H)}
    print("PASS", flush=True)
stackweave.run(H, main)
''')


def test_identity_warnings_lock_released_after_a_migrating_showwarning():
    """A showwarning hook that does I/O (logging.captureWarnings with a
    network handler, say) parks and resumes on another hub, then parks again
    while a fiber on every hub warns.  Every warning returns, and the waiters
    parked (stats()["fiber_lock_parks"]) instead of blocking their hubs.

    Was a gap, twice over: C do_warn holds the interpreter's warnings lock (a
    _PyRecursiveMutex) across showwarning.  Owned by the OS thread, it could
    not be released after the move and every later warn hung; the exec-home
    lock.c hunk owns it by the thread state.  And a warn that had to wait
    blocked its hub's OS thread, so with a waiter on every hub nothing could
    resume the holder; _PyRecursiveMutex_Lock now waits with _PY_LOCK_FIBER,
    which parks the fiber."""
    run_gap(r'''
import warnings
_watchdog(20)
H = 4
assert stackweave_c.rlock_tstate_owned and stackweave_c.fiber_locks_installed
warnings.simplefilter("always")
state = {"first": True}
done = []
started = set()
waited = {}
wg = stackweave.WaitGroup(); wg.add(H)
def parks():
    return stackweave_c.stats()["fiber_lock_parks"]
def other(h):
    started.add(h)
    warnings.warn("on hub %d" % h, DeprecationWarning)
    done.append(h); wg.done()
def show(*a, **k):
    if state["first"] and stackweave_c.current_g() is not None:
        state["first"] = False
        before = threading.get_ident()
        force_migrate(rounds=10)
        if threading.get_ident() != before:
            print("REACHED: showwarning resumed on another hub", flush=True)
        p0 = parks()
        for h in range(H):                 # one waiter per hub, lock held
            stackweave_c.mn_fiber(lambda h=h: other(h), hub=h)
        end = time.monotonic() + 10        # sleeps, not lock waits: only the
        while len(started) < H and time.monotonic() < end:
            stackweave.sleep(0.01)         # waiters' parks count below
        stackweave.sleep(0.2)
        waited["parks"] = parks() - p0
        print("waiters parked: %d" % waited["parks"], flush=True)
warnings.showwarning = show
def main():
    warnings.warn("first", UserWarning)
    wg.wait()
    print("warned on hubs", sorted(done), flush=True)
    assert sorted(done) == list(range(H)), done
    assert waited["parks"] >= H, "the waiters never parked: %r" % (waited,)
    print("PASS", flush=True)
stackweave.run(H, main)
''')


def test_identity_import_lock_released_after_a_migrating_finder():
    """A meta-path finder that does I/O (setuptools' editable finder calls
    Path.exists) parks and resumes on another hub, then parks again while a
    fiber on every hub imports.  The import fails with ModuleNotFoundError,
    the imports on every hub work, and their waits parked
    (stats()["fiber_lock_parks"]) instead of blocking their hubs.

    Was a gap, twice over: importlib runs every find_spec() under the global
    import lock (_imp, a _PyRecursiveMutex).  Owned by the OS thread, the
    finder got 'not holding the import lock' after the move and the lock
    leaked, so every later import hung; the exec-home lock.c hunk owns it by
    the thread state.  And an import that had to wait blocked its hub's OS
    thread, so with a waiter on every hub nothing could resume the finder;
    _PyRecursiveMutex_Lock now waits with _PY_LOCK_FIBER, which parks the
    fiber."""
    run_gap(r'''
import importlib, importlib.abc
_watchdog(20)
H = 4
assert stackweave_c.rlock_tstate_owned and stackweave_c.fiber_locks_installed
done = []
started = set()
waited = {}
wg = stackweave.WaitGroup(); wg.add(H)
def parks():
    return stackweave_c.stats()["fiber_lock_parks"]
def other(h):
    started.add(h)
    importlib.import_module(["colorsys", "fractions", "statistics", "difflib"][h])
    done.append(h); wg.done()
class ParkingFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == "nosuch_mod_for_the_finder" and stackweave_c.current_g() is not None:
            before = threading.get_ident()
            force_migrate(rounds=5)
            if threading.get_ident() != before:
                print("REACHED: find_spec resumed on another hub", flush=True)
            p0 = parks()
            for h in range(H):             # one waiter per hub, lock held
                stackweave_c.mn_fiber(lambda h=h: other(h), hub=h)
            end = time.monotonic() + 10    # sleeps, not lock waits: only the
            while len(started) < H and time.monotonic() < end:
                stackweave.sleep(0.01)     # waiters' parks count below
            stackweave.sleep(0.2)
            waited["parks"] = parks() - p0
            print("waiters parked: %d" % waited["parks"], flush=True)
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
    wg.wait()
    print("imported on hubs", sorted(done), flush=True)
    assert first == "ModuleNotFoundError", first
    assert sorted(done) == list(range(H)), done
    assert waited["parks"] >= H, "the waiters never parked: %r" % (waited,)
    print("PASS", flush=True)
stackweave.run(H, main)
''')


@pytest.mark.parametrize("patched", [False, True], ids=["stock", "monkey"])
def test_identity_mp_value_lock_reenters_after_a_migration(patched):
    """`with v.get_lock(): <park, migrate>; v.value += 1` -- the property
    re-acquires the same recursive lock, which must still count as held, a
    fiber on the holder's hub must not count as its owner, and the release
    after the migration must free it.  multiprocessing is imported after
    monkey.patch(), the usual order.

    Was a gap: a recursive SemLock decided 'mine' by the OS thread
    (semaphore.c ISMINE), so after a migration the holder's re-acquire waited
    on itself in sem_wait, with or without monkey.patch.  Stock: the
    exec-home semaphore.c hunk keys it on the thread state, as lock.c does.
    Under monkey.patch: executors._CoRecursiveSemLock keeps owner and depth
    per fiber."""
    run_gap(r'''
_watchdog(25)
if PATCHED:
    import stackweave.monkey as monkey; monkey.patch()
import multiprocessing as mp
assert stackweave_c.rlock_tstate_owned
def main():
    v = mp.Value("i", 0)
    sem = v.get_lock()._semlock
    if PATCHED:
        # the fiber-keyed recursive lock is the one in use
        assert type(sem).__name__ == "_CoRecursiveSemLock", type(sem)
    seen = {}
    with v.get_lock():
        before = threading.get_ident()
        ch = stackweave.Chan(1)
        def peer():                        # another fiber on the holder's hub
            ch.send((threading.get_ident(), sem._is_mine()))
        stackweave_c.mn_fiber(peer, hub=stackweave_c.mn_current_hub())
        (tid, seen["peer"]), _ = ch.recv()
        assert tid == before, "the peer ran on another OS thread"
        before = threading.get_ident()
        force_migrate(rounds=20)
        if threading.get_ident() != before:
            print("REACHED: moved to another hub holding the lock", flush=True)
        seen["holder"] = sem._is_mine()
        v.value += 1
        assert sem._count() == 1
    print("is_mine:", seen, flush=True)
    assert seen == {"peer": False, "holder": True}, seen
    assert v.get_lock().acquire(timeout=2)
    v.get_lock().release()
    # released for real: a plain OS thread can take it now
    got = []
    def plain_thread():
        got.append(v.get_lock().acquire(timeout=2))
        v.get_lock().release()
    t = threading.Thread(target=plain_thread)
    t.start(); t.join()
    assert got == [True], got
    print("value", v.value, flush=True)
    print("PASS", flush=True)
stackweave.run(2, main)
'''.replace("PATCHED", repr(patched)))


@pytest.mark.parametrize("raw", ["socket-park", "python-raw-preempt"])
def test_identity_shared_buffered_stream_waits_on_one_hub(raw):
    """Two fibers on one hub use one buffered stream; the first is inside the
    raw stream's Python code (a cooperative recv that parks, or a long
    write that is preempted).  The second must wait, as a thread would.

    Was a gap: _io.Buffered* kept its lock's owner as the OS thread
    (_enter_buffered_busy), so the second got 'reentrant call inside'.  The
    exec-home bufferedio.c hunk owns the lock by the thread state, and makes
    it a PyMutex whose wait parks the fiber (stats()["fiber_lock_parks"]): a
    wait that blocked the hub's OS thread would never let a preempted holder
    run again."""
    run_gap(r'''
import io, time as _t
_watchdog(40)
assert stackweave_c.fiber_locks_installed
RAW = "RAWKIND"
if RAW == "socket-park":
    import stackweave.monkey as monkey; monkey.patch()
import socket
inside = []
P0 = stackweave_c.stats()["fiber_lock_parks"]
def parked():                               # the second fiber waits on the lock
    return stackweave_c.stats()["fiber_lock_parks"] > P0
def _step(end):
    return _t.perf_counter() < end
class SpinRaw(io.RawIOBase):
    def writable(self): return True
    def write(self, b):
        inside.append(1)
        end = _t.perf_counter() + 10        # spin (preempted, not parked) until
        while _step(end) and not parked():  # the second waits, capped
            pass
        inside.pop()
        return len(b)
class MarkedSocketIO(socket.SocketIO):
    def readinto(self, b):
        inside.append(1)
        try:
            return super().readinto(b)      # parks in the patched recv
        finally:
            inside.pop()
def main():
    if RAW == "socket-park":
        a, b = socket.socketpair()
        f = io.BufferedReader(MarkedSocketIO(a, "rb"))
        op = lambda: f.read(4)
    else:
        f = io.BufferedWriter(SpinRaw(), buffer_size=4)
        op = lambda: (f.write(b"x" * 8), f.flush())
    res = {}
    wg = stackweave.WaitGroup(); wg.add(2)
    def worker(i):
        if i == 1 and inside:
            print("REACHED: second fiber starts while the first is inside the raw stream", flush=True)
        try:
            op(); res[i] = "ok"
        except Exception as e:
            res[i] = repr(e)
        wg.done()
    def wait_for(cond):
        end = _t.monotonic() + 10
        while not cond() and _t.monotonic() < end:
            stackweave.sleep(0.005)
    stackweave_c.mn_fiber(lambda: worker(0), hub=0)
    wait_for(lambda: inside)                # the first is inside the raw stream
    stackweave_c.mn_fiber(lambda: worker(1), hub=0)
    if RAW == "socket-park":
        wait_for(lambda: parked() or 1 in res)
        b.sendall(b"onetwo!!")
    wg.wait()
    print(res, flush=True)
    assert res == {0: "ok", 1: "ok"}, res
    assert parked(), "the second never parked"
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


def test_identity_asyncio_debug_loop_keeps_working_after_a_migration():
    """asyncio.run(debug=True) (as -X dev and IsolatedAsyncioTestCase use)
    inside a fiber whose loop moves between hubs keeps scheduling callbacks:
    under monkey.patch the loop's debug thread check compares the fiber that
    runs it (monkey/aioloop.py), and a different fiber is still refused."""
    run_gap(r'''
import stackweave.monkey as monkey; monkey.patch()
import asyncio, logging
logging.getLogger("asyncio").setLevel(logging.CRITICAL)   # slow-callback noise
_watchdog(20)
from stackweave.monkey import aioloop
checks = [0]
_orig_check = asyncio.base_events.BaseEventLoop._check_thread
def counting_check(self):
    checks[0] += 1
    return _orig_check(self)
asyncio.base_events.BaseEventLoop._check_thread = counting_check
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
            loop = asyncio.get_running_loop()
            assert loop._stackweave_fiber is not None, "loop not keyed by fiber"
            seen = set()
            for _ in range(300):
                await asyncio.sleep(0.001)
                seen.add(threading.get_ident())
                loop.call_soon(lambda: None)
            # another fiber is another thread of control: still refused
            refused = stackweave.Chan(1)
            def intruder():
                try:
                    loop.call_soon(lambda: None)
                    refused.send(False)
                except RuntimeError:
                    refused.send(True)
            stackweave.fiber(intruder)
            res["intruder_refused"] = refused.recv()[0]   # (value, ok)
            return len(seen)
        try:
            res["os_threads"] = asyncio.run(co(), debug=True)
        except RuntimeError as e:
            res["error"] = str(e)
    done = stackweave.Chan(1)
    stackweave.fiber(lambda: (runner(), done.send(1)))
    done.recv()
    print(res, "checks", checks[0], flush=True)
    if res.get("os_threads", 0) > 1 or "other than the current one" in res.get("error", ""):
        print("REACHED: the loop's fiber ran on more than one hub", flush=True)
    assert "error" not in res, res["error"]
    assert checks[0] >= 300, checks      # the debug check ran on every call_soon
    assert res["intruder_refused"] is True, res
    print("PASS", flush=True)
stackweave.run(4, main)
''')


def test_identity_thread_started_in_a_fiber_is_joined_at_exit():
    """A threading.Thread started from a fiber is non-daemon, as one started
    from the main thread is, so the interpreter waits for it at exit
    (stackweave.runtime gives a fiber's dummy thread the daemon flag of the
    thread that called run()).  An explicit daemon=True still wins."""
    run_gap(r'''
import time as _t
def main():
    cur = threading.current_thread()
    assert type(cur).__name__ == "_DummyThread" and cur._stackweave_fiber_dummy, cur
    assert cur.daemon is False, cur
    def work():
        _t.sleep(0.3)
        print("PASS", flush=True)
    t = threading.Thread(target=work)
    t.start()
    d = threading.Thread(target=_t.sleep, args=(60,), daemon=True)
    d.start()                 # would hold up exit for 60 s if it were joined
    print("REACHED: thread started from a fiber, daemon=%s" % t.daemon, flush=True)
    assert t.daemon is False and d.daemon is True
stackweave.run(2, main)
''')


def test_identity_patched_os_read_leaves_the_fd_mode_alone():
    """Cooperative os.read/os.write/readv/writev/readinto from fibers leave a
    blocking fd blocking, as they found it, for whatever else shares the file
    description (a pipe, a pty, a socketpair) -- and still park the fiber, not
    the hub: a reader and a writer pinned to ONE hub both finish, which they
    could not if the read blocked that hub's OS thread."""
    run_gap(r'''
import socket, select
import stackweave.monkey as monkey; monkey.patch()
_watchdog(40)
def main():
    r, w = os.pipe()
    m, s = os.openpty()
    a, b = socket.socketpair()
    fa, fb = a.detach(), b.detach()
    fds = (r, w, m, s, fa, fb)
    assert all(os.get_blocking(fd) for fd in fds)
    big = os.urandom(3 * select.PIPE_BUF + 1)
    got, wrote = [], []
    done = stackweave.Chan(2)
    def reader():               # parks on the empty blocking pipe first
        buf = b""
        while len(buf) < len(big) + 1:
            buf += os.read(r, 1 << 16)
        got.append(buf)
        done.send(1)
    def writer():
        stackweave.sleep(0.05)
        wrote.append(os.write(w, big))       # the whole buffer, as blocking
        wrote.append(os.write(w, b"!"))
        done.send(1)
    stackweave_c.mn_fiber(reader, hub=0)
    stackweave_c.mn_fiber(writer, hub=0)
    done.recv(); done.recv()
    assert wrote == [len(big), 1], wrote
    assert got == [big + b"!"], "pipe data mismatch"
    print("REACHED: patched os.read/os.write parked a fiber on one hub", flush=True)
    assert os.writev(w, [b"ab", b"cd"]) == 4
    ba = bytearray(3)
    assert os.readinto(r, ba) == 3 and ba == b"abc"
    bb = bytearray(1)
    assert os.readv(r, [bb]) == 1 and bb == b"d"
    assert os.write(m, b"line\n") == 5 and os.read(s, 64) == b"line\n"
    assert os.write(s, b"back") == 4 and os.read(m, 64).endswith(b"back")
    def sock_writer():
        wrote.append(os.write(fa, b"x" * 100000))
        done.send(1)
    stackweave.fiber(sock_writer)
    n = 0
    while n < 100000:
        n += len(os.read(fb, 1 << 16))
    done.recv()
    assert wrote[-1] == 100000, wrote
    modes = {fd: os.get_blocking(fd) for fd in fds}
    print("blocking after:", modes, flush=True)
    assert all(modes.values()), modes
    print("PASS", flush=True)
stackweave.run(2, main)
''')


def test_identity_child_forked_from_a_fiber_runs_signal_handlers():
    """A child forked from a fiber runs a Python signal handler for an
    asynchronous signal (setitimer) while it executes bytecode.  CPython
    never updates _PyRuntime.main_tstate after fork, and a signal sets its
    pending bit there; every M:N fork comes from a fiber's own state on a
    hub thread, so the bit landed on the old main thread's (deleted) state
    and the handler ran only at the next EINTR.  The after-fork reset now
    makes the fiber's state the main one: `_main_thread_state()` checks
    that it is, and that CPython's main thread is this one too."""
    run_gap(r'''
import signal, time as _t, warnings
warnings.simplefilter("ignore", DeprecationWarning)   # fork with threads
def child():
    hits = []
    main_state = stackweave_c._main_thread_state()
    own_heap = stackweave_c._alloc_home_is_own()
    signal.signal(signal.SIGALRM, lambda *a: hits.append(_t.perf_counter()))
    t0 = _t.perf_counter()
    signal.setitimer(signal.ITIMER_REAL, 0.05)
    os.write(1, b"REACHED: child armed an async SIGALRM\n")
    while _t.perf_counter() < t0 + 1.0 and not hits:
        pass
    # Decide before any syscall: os.write checks pending signals itself, which
    # would run a handler the bytecode loop never ran.
    ran_in_loop = bool(hits)
    os.write(1, ("child handler ran during the loop: %s; main thread state "
                 "(tstate, thread) is mine: %r; own heap: %r\n"
                 % (ran_in_loop, main_state, own_heap)).encode())
    os._exit(0 if ran_in_loop and main_state[:2] == (True, True)
             and own_heap is not False else 1)
def main():
    assert stackweave_c._alloc_home_is_own() is not True   # borrows its hub's
    pid = os.fork()
    if pid == 0:
        child()
    _, st = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(st) == 0, \
        "the handler never ran during the loop, or the main state is not the fiber's"
    print("PASS", flush=True)
stackweave.run(2, main)
''')


def test_identity_fork_children_never_name_a_freed_main_state():
    """CPython's main thread state in later fork generations.  In a child
    forked from a fiber the fiber's state is main (above).  A thread started
    there takes the memory of the old main state (the fork reset it for
    reuse), and main must not name that thread's state.  A grandchild forked
    from that plain thread loses the fiber's state (the fork deletes every
    state but the forking one), so main must not go on naming it: every
    later signal would write into freed memory.  It names the forking
    thread's state or the embedded initial one, as upstream."""
    run_gap(r'''
import warnings
warnings.simplefilter("ignore", DeprecationWarning)   # fork with threads
def child():
    seen = {"child": stackweave_c._main_thread_state()}
    def plain_thread():
        seen["thread"] = stackweave_c._main_thread_state()
        pid = os.fork()
        if pid == 0:
            mine, _, initial = stackweave_c._main_thread_state()
            os._exit(0 if mine or initial else 1)
        seen["grandchild_rc"] = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
    t = threading.Thread(target=plain_thread)
    t.start(); t.join()
    seen["child_after"] = stackweave_c._main_thread_state()
    os.write(1, ("REACHED: grandchild forked from a plain thread in a fiber's "
                 "child: %r\n" % (seen,)).encode())
    ok = (seen["child"][:2] == (True, True)
          and seen["thread"][0] is False
          and seen["grandchild_rc"] == 0
          and seen["child_after"][:2] == (True, True))
    os._exit(0 if ok else 1)
def main():
    pid = os.fork()
    if pid == 0:
        child()
    _, st = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(st) == 0, "a fork child's main state is wrong"
    print("PASS", flush=True)
stackweave.run(2, main)
''')


# ---------------------------------------------------------------------------
# preempt: a lock held by a fiber that was preempted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lock", ["threading.Lock", "_strptime._cache_lock"])
def test_preempt_lock_holder_runs_while_a_same_hub_fiber_waits(lock):
    """Fiber A runs CPU-bound Python inside a stock lock and is preempted; B
    on the same hub then wants the lock.  A finishes and B gets it, as with
    threads.  `_strptime._cache_lock` is taken implicitly by time.strptime.

    Was a gap: a preempted fiber waits on its hub's ready ring, which nothing
    steals, and B's wait blocked the hub's OS thread, so A never ran again.
    The exec-home parking_lot.c hunk lets a _thread lock's wait park the
    fiber (stats()["fiber_lock_parks"]), whenever the lock was made."""
    run_gap(r'''
import time as _t, _strptime
_watchdog(45)
assert stackweave_c.fiber_locks_installed
LOCK = "LOCKNAME"
lock = threading.Lock() if LOCK == "threading.Lock" else _strptime._cache_lock
held, b_started = [False], [False]
def step(end):
    return _t.perf_counter() < end and not b_started[0]
def main():
    out = {}
    wg = stackweave.WaitGroup(); wg.add(2)
    def A():
        # Hold the lock, spinning, until B has started.  B is on this hub, so
        # it can only start while A spins if A was preempted holding the lock:
        # the trigger cannot be missed on a slow box (20 s cap).
        with lock:
            held[0] = True
            end = _t.perf_counter() + 20
            while step(end):
                pass
            held[0] = False
        out["A"] = "done"; wg.done()
    def B():
        b_started[0] = True
        if held[0]:
            print("REACHED: B runs on the hub while preempted A holds the lock", flush=True)
        t0 = _t.perf_counter()
        if LOCK == "threading.Lock":
            with lock:
                pass
        else:
            _t.strptime("2026-11-08", "%Y-%m-%d")
        out["B_waited"] = round(_t.perf_counter() - t0, 3); wg.done()
    stackweave_c.mn_fiber(A, hub=0)
    while not held[0]:
        stackweave.sleep(0.005)
    stackweave_c.mn_fiber(B, hub=0)
    wg.wait()
    print(out, flush=True)
    assert stackweave_c.stats()["fiber_lock_parks"] >= 1, "B never parked"
    print("PASS", flush=True)
stackweave.run(2, main)
'''.replace("LOCKNAME", lock))


@pytest.mark.parametrize("lock", ["_strptime._cache_lock", "logging-handler"])
def test_preempt_preimported_stdlib_lock_holder_runs_under_monkey(lock):
    """The same scenario under monkey.patch, for stdlib locks made BEFORE
    patch() with the real factories (the module imported first, the handler
    created first): patch() re-creates them cooperative
    (_fixup_preimported_stdlib_locks), so the same-hub waiter parks and the
    preempted holder finishes.  unpatch() puts the originals back.

    The trigger does not hang on timing: B is spawned only once A holds the
    lock, and A spins inside it until B has started, which on hub 0 can only
    happen once A has been preempted.  A pinned fiber on a busy hub can wait
    ~64 time slices for its turn (finding 14), so A allows 20 s for that."""
    run_gap(r'''
import time as _t, _strptime, logging, io
held, b_started, emitted_while_held = [False], [False], []
class Probe(io.StringIO):
    def write(self, s):                       # runs under handler.lock
        emitted_while_held.append(held[0])
        return super().write(s)
handler = logging.StreamHandler(Probe())     # real RLock: made pre-patch
log = logging.getLogger("preimported"); log.addHandler(handler); log.propagate = False
orig = {"_strptime": _strptime._cache_lock, "handler": handler.lock}
import stackweave.monkey as monkey; monkey.patch()
_watchdog(45)
LOCK = "LOCKNAME"
lock = _strptime._cache_lock if LOCK == "_strptime._cache_lock" else handler.lock
assert type(lock).__name__ in ("CoLock", "CoRLock"), type(lock)
def main():
    out = {}
    wg = stackweave.WaitGroup(); wg.add(2)
    def A():
        with lock:
            held[0] = True
            end = _t.perf_counter() + 20
            while not b_started[0] and _t.perf_counter() < end:
                pass                          # CPU-bound: only preemption lets B in
            held[0] = False
            out["A_released"] = _t.perf_counter()
        wg.done()
    def B():
        b_started[0] = True
        if held[0]:
            print("REACHED: B runs on the hub while preempted A holds the lock", flush=True)
        if LOCK == "_strptime._cache_lock":
            _t.strptime("2026-11-08", "%Y-%m-%d")
        else:
            log.warning("from B")
        out["B_done"] = _t.perf_counter(); wg.done()
    stackweave_c.mn_fiber(A, hub=0)
    deadline = _t.perf_counter() + 20
    while not held[0] and _t.perf_counter() < deadline:
        stackweave.sleep(0.005)
    stackweave_c.mn_fiber(B, hub=0)
    wg.wait()
    print(out, "emitted while A held:", emitted_while_held, flush=True)
    assert out["B_done"] > out["A_released"], "B got the lock while A held it"
    assert True not in emitted_while_held, "B got into the handler lock A held"
    finished.append(True)
finished = []
stackweave.run(2, main)
assert finished, "main did not finish"
monkey.unpatch()
assert _strptime._cache_lock is orig["_strptime"] and handler.lock is orig["handler"]
print("PASS", flush=True)
'''.replace("LOCKNAME", lock))


def test_preimported_stdlib_lock_held_by_the_patching_thread_stays_real():
    """patch() run while the calling thread holds a pre-patch RLock (say,
    from a logging handler factory under dictConfig) must not swap it: the
    holder would then release the new, unheld lock.  acquire(False) used to
    be the "is it held?" test, and it succeeds re-entrantly for the owner.
    A later patch() call, once it is released, swaps it."""
    rc, out, err = run_scenario(r'''
import logging, io
real_rlock = type(logging._lock)          # patch() rebinds _thread.RLock
handler = logging.StreamHandler(io.StringIO())
handler.acquire()                         # held by this thread across patch()
with logging._lock:
    import stackweave.monkey as monkey
    monkey.patch()
    assert type(logging._lock) is real_rlock, logging._lock
    assert type(handler.lock) is real_rlock, handler.lock
handler.release()                         # the lock it took: no RuntimeError
monkey.patch()                            # released now: swapped
assert type(handler.lock).__name__ == "CoRLock", handler.lock
assert type(logging._lock).__name__ == "CoRLock", logging._lock
handler.acquire(); handler.release()
print("PASS", flush=True)
''', timeout=60)
    assert rc == 0 and "PASS" in out, (out, err[-3000:])


def test_preempt_sqlite_writer_waits_for_a_same_hub_transaction():
    """Was a gap (fixed under monkey.patch: monkey/sqlite.py moves the wait
    out of sqlite3_step): sqlite's busy handler slept inside sqlite3_step on
    the hub's OS thread, so a fiber holding the write lock across a park
    could not run (its wake timer is on that hub) until the waiter's timeout
    expired.  One connection per fiber on one database file, both fibers
    pinned to one hub.  A holds the write lock across a short park; B writes.
    B waits for A's commit (~50 ms) and succeeds, as a thread would.  Without
    monkey.patch the wait still blocks the hub, as any blocking C call does."""
    run_gap(r'''
import stackweave.monkey as monkey; monkey.patch()
import sqlite3, tempfile, time as _t
from stackweave.monkey import sqlite as sw_sqlite
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
    waits = sw_sqlite.cooperative_waits
    stackweave_c.mn_fiber(holder, hub=0)
    stackweave.sleep(0.01)
    stackweave_c.mn_fiber(writer, hub=0)
    wg.wait()
    out["coop_waits"] = sw_sqlite.cooperative_waits - waits
    rows = sqlite3.connect(path).execute("select x from t order by x").fetchall()
    print(out, rows, flush=True)
    assert out["B"] == "ok" and out["B_waited"] < 1.0, out
    assert out["coop_waits"] > 0, "B never took the cooperative busy wait"
    assert rows == [(1,), (2,)], rows
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

def test_sched_fiber_pinned_onto_a_busy_hub_gets_a_turn_promptly():
    """A fiber does `g.pin(0); sleep(0)` while hub 0 runs a CPU-bound fiber;
    it first runs on hub 0 within a few time slices (well under 1 s).  It
    waits on the global run-queue, which hub 0 used to pull only every
    starve_bound (64) ready-ring turns while the preempted hog refilled the
    ring: ~64 x 50 ms.  A preemption now gives that queue a turn first;
    stats()["mn_preempt_turns"] counts those turns."""
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
turns = stackweave_c.stats()["mn_preempt_turns"]
stackweave.run(2, main)
turns = stackweave_c.stats()["mn_preempt_turns"] - turns
print("preempt turns", turns, flush=True)
assert turns > 0, "no hub ran waiting work ahead of a fiber it preempted"
print("PASS", flush=True)
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
