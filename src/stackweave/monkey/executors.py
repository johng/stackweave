"""Goroutine-backed concurrent.futures + multiprocessing cooperation."""
from ._base import *  # noqa: F401,F403  (shared foundation)
from .events import CoEvent, CoSemaphore  # noqa: F401
from . import osio  # _orig_os_read/_orig_os_write are rebound at patch-time -> read live


def _spawn(fn):
    """Spawn the worker fiber on whichever scheduler is active: mn_fiber under
    M:N (mn_hub_count() > 0), else the single-thread go.  A task spawned via the
    single-thread fiber() never runs under mn_run, so future.result() would hang."""
    if stackweave_c.mn_spawns_to_hubs():   # not inside a nested run(1)
        return stackweave_c.mn_fiber(fn)
    return stackweave_c.fiber(fn)

# ============================================================
# concurrent.futures -- fiber-backed ThreadPoolExecutor
#
# The stock ThreadPoolExecutor runs work on real OS threads and resolves each
# Future by notifying its threading.Condition from the worker thread.  After
# patch() that Condition is a CoCondition, and a cross-thread notify of a
# cooperative waiter does not wake it -- a fiber calling future.result()
# deadlocks (and the worker's work_queue.get() blocks on a CoSimpleQueue from
# a non-fiber thread).  Real-threaded executors are fundamentally at odds
# with cooperative primitives: the worker side needs real locks, the fiber
# caller side needs cooperative ones, and Future._condition cannot be both.
#
# So make ThreadPoolExecutor fiber-backed: submitted work runs as a
# fiber on the cooperative scheduler.  Both the setter (the task) and the
# waiter (future.result()/wait()/as_completed()) are then in the fiber
# domain, so the stock Future's CoCondition works.  Work is cooperative rather
# than parallel; a task that makes a truly-blocking call still offloads via the
# patched blocking APIs.  max_workers bounds concurrency with a CoSemaphore.
#
# Future.result/wait/as_completed themselves need no patch -- they already
# cooperate whenever the Future is completed from within a fiber.
# ============================================================
_orig_ThreadPoolExecutor = None
_co_tpe_cls = None

# Real (never-patched) lock for the executor's O(1) bookkeeping; captured at
# import time -- before patch() rebinds _thread.allocate_lock to the cooperative
# CoLock -- so it stays a real OS mutex.
# Held only for set/flag updates, never across a park.
_raw_allocate_lock = _thread.allocate_lock


def _build_co_tpe():
    import concurrent.futures as cf
    try:
        from concurrent.futures.thread import BrokenThreadPool as _BrokenThreadPool
    except Exception:                       # pragma: no cover - ancient Python
        _BrokenThreadPool = RuntimeError

    class CoThreadPoolExecutor(cf.Executor):
        """ThreadPoolExecutor that runs submitted callables as fibers."""

        def __init__(self, max_workers=None, thread_name_prefix="",
                     initializer=None, initargs=()):
            if max_workers is None:
                max_workers = min(32, (os.cpu_count() or 1) + 4)
            if max_workers <= 0:
                raise ValueError("max_workers must be greater than 0")
            self._max_workers = max_workers
            self._sem = CoSemaphore(max_workers)
            self._initializer = initializer
            self._initargs = initargs
            self._shutdown = False
            self._broken = None             # BrokenThreadPool once init fails
            self._guard = _raw_allocate_lock()   # real lock, O(1) bookkeeping
            # Only the IN-FLIGHT futures (submitted but not finished): each task
            # discards its own future on completion, so the set stays
            # O(in-flight) -- never O(total submits) -- and shutdown() has the
            # not-yet-started futures to cancel.
            self._pending = set()
            # Set whenever _pending is empty; shutdown(wait=True) parks on it
            # until every task fiber has run its finally (i.e. after set_result
            # and its inline done-callbacks), matching stdlib join semantics.
            self._idle = CoEvent()
            self._idle.set()
            # The initializer runs once per POOL (a fiber-backed pool has no
            # persistent worker threads), not once per task.
            self._init_started = False
            self._init_done = CoEvent()

        def _run_initializer(self):
            """Run the pool initializer exactly once for this executor, before
            the first task's callable runs.  stdlib runs it once per worker
            THREAD; the fiber-backed pool has no persistent workers, so
            once-per-pool is the faithful, bounded analogue -- unlike the old
            once-per-TASK, which re-ran per-worker setup (DB connect, license
            slot, thread-local cache) for every submit.  A task that arrives
            while another fiber is initializing waits on _init_done; if the
            initializer raised, the pool is broken and every task/submit
            surfaces BrokenThreadPool, as stdlib does."""
            if self._initializer is None:
                return
            if self._init_done.is_set():    # fast path once settled
                if self._broken is not None:
                    raise self._broken
                return
            do_init = False
            with self._guard:
                if self._broken is not None:
                    raise self._broken
                if self._init_done.is_set():
                    return
                if not self._init_started:
                    self._init_started = True
                    do_init = True
            if not do_init:
                # Another fiber owns the one-shot init; wait for it to finish.
                self._init_done.wait()
                if self._broken is not None:
                    raise self._broken
                return
            try:
                self._initializer(*self._initargs)
            except BaseException as exc:
                broken = _BrokenThreadPool(
                    "A worker initializer raised in CoThreadPoolExecutor")
                broken.__cause__ = exc
                with self._guard:
                    self._broken = broken
                self._init_done.set()       # wake waiters -> they see _broken
                raise broken
            self._init_done.set()

        def _task_done(self, fut):
            with self._guard:
                self._pending.discard(fut)
                if not self._pending:
                    self._idle.set()

        def submit(self, fn, /, *args, **kwargs):
            if self._shutdown:
                raise RuntimeError(
                    "cannot schedule new futures after shutdown")
            if self._broken is not None:
                raise self._broken
            fut = cf.Future()
            with self._guard:
                # Re-check under the guard so a concurrent shutdown() cannot race
                # a fresh future into _pending after it has snapshotted the set.
                if self._shutdown:
                    raise RuntimeError(
                        "cannot schedule new futures after shutdown")
                if self._broken is not None:
                    raise self._broken
                self._pending.add(fut)
                self._idle.clear()
            sem = self._sem

            def task():
                sem.acquire()           # honour max_workers
                try:
                    if not fut.set_running_or_notify_cancel():
                        return          # future was cancelled before start
                    try:
                        self._run_initializer()   # once per pool
                        result = fn(*args, **kwargs)
                    except BaseException as exc:
                        fut.set_exception(exc)
                    else:
                        fut.set_result(result)
                finally:
                    sem.release()
                    self._task_done(fut)

            # A fiber-spawned task only runs while a scheduler is driving it.
            # With no runtime live (a plain main thread before/after run(), a
            # background OS thread with no hub), run it inline so the future
            # still resolves -- stock ThreadPoolExecutor works in any context.
            if _runtime_live():
                _spawn(task)
            else:
                task()
            return fut

        def shutdown(self, wait=True, *, cancel_futures=False):
            with self._guard:
                self._shutdown = True
                pending = list(self._pending) if cancel_futures else ()
            if cancel_futures:
                # Cancel every not-yet-started future.  A running future refuses
                # the cancel (Future.cancel() -> False) and finishes normally; a
                # cancelled one is skipped by set_running_or_notify_cancel().
                for fut in pending:
                    fut.cancel()
            if wait:
                self._idle.wait()

    return CoThreadPoolExecutor


def _patch_futures():
    global _orig_ThreadPoolExecutor, _co_tpe_cls
    try:
        import concurrent.futures as cf
    except ImportError:
        return
    if _co_tpe_cls is None:
        _co_tpe_cls = _build_co_tpe()
    # Touch the attribute first so the lazy submodule import (and its
    # os.register_at_fork) runs against the real class, then override.
    _orig_ThreadPoolExecutor = cf.ThreadPoolExecutor
    cf.ThreadPoolExecutor = _co_tpe_cls
    thread_mod = sys.modules.get("concurrent.futures.thread")
    if thread_mod is not None:
        thread_mod.ThreadPoolExecutor = _co_tpe_cls


def _unpatch_futures():
    if _orig_ThreadPoolExecutor is None:
        return
    import concurrent.futures as cf
    cf.ThreadPoolExecutor = _orig_ThreadPoolExecutor
    thread_mod = sys.modules.get("concurrent.futures.thread")
    if thread_mod is not None:
        thread_mod.ThreadPoolExecutor = _orig_ThreadPoolExecutor


# ============================================================
# multiprocessing -- cooperate regardless of import order
#
# multiprocessing.connection.Connection._recv / _send (POSIX) capture os.read /
# os.write as *default arguments* at import time:
#
#     _read = os.read
#     def _recv(self, size, read=_read): ...
#
# If multiprocessing was imported before patch(), those defaults are the
# original *blocking* os.read/os.write -- so Connection.recv/send (and
# everything built on Connection: Pipe, Queue, Pool, Process.join's sentinel
# wait) does a blocking read that freezes the whole scheduler instead of
# parking on wait_fd.  The data still flows, so it "works", but it doesn't
# yield.  Rebind the captured defaults (and the module _read/_write) to the
# now-patched cooperative os.read/os.write.  Runs after the "os" patch, so
# os.read/os.write already point at the cooperative versions.  If the module
# isn't imported yet, it's a no-op -- a later import binds the patched os.read.
#
# Connection._close(self, _close=os.close) captures os.close the same way:
# the patched os.close clears the fd's netpoll registration bit so a reused fd
# re-arms cleanly under the edge-triggered register-once scheme, but the
# original os.close does not -- so a Connection closed with the unpatched
# default leaves a stale registration, and the next Connection that reuses that
# fd number never wakes from wait_fd (a hang).  Rebind it too.
# ============================================================
_orig_mp_recv_defaults  = None
_orig_mp_send_defaults  = None
_orig_mp_close_defaults = None
# Every SemLock class patched, with what it had before:
# [(cls, _make_methods, __enter__ or None, __exit__ or None)].  More than one
# when multiprocessing.synchronize is reloaded or re-imported after patch().
_patched_semlock_classes = []

# multiprocessing.synchronize.RECURSIVE_MUTEX -- the SemLock.kind of an RLock
# (Value/Array.get_lock()); SEMAPHORE (1) is Lock/Semaphore/Event/Condition.
_SEMLOCK_RECURSIVE = 0
_SEMLOCK_SEMAPHORE = 1


def _semlock_backoff_acquire(try_acquire, timeout):
    """Poll try_acquire() with a cooperative backoff park until it returns True
    or `timeout` (None = forever) runs out.  POSIX semaphores have no readiness
    fd, so this is a backoff poll -- same shape as the fcntl shim."""
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    step = 0.0005
    while True:
        if try_acquire():
            return True
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _co_sleep(min(step, remaining))
        else:
            _co_sleep(step)
        if step < 0.02:
            step *= 2


def _co_semlock_acquire(semlock):
    """Cooperative SemLock.acquire.  A blocking acquire from a fiber does a
    non-blocking sem_trywait + _co_sleep backoff instead of sem_wait, so a
    contended cross-process Lock/Semaphore/Event/Condition/Barrier doesn't
    freeze the scheduler thread.  Real threads and explicit non-blocking
    acquires fall straight through to the C call."""
    def acquire(block=True, timeout=None):
        if not block or not _in_fiber():
            return semlock.acquire(block, timeout)
        return _semlock_backoff_acquire(lambda: semlock.acquire(False), timeout)
    return acquire


# Twins (below) that share the HANDLE of an unnamed recursive SemLock -- the
# "fork" start method unlinks the name at creation, so there is nothing to
# sem_open again.  Kept for the life of the process: a C SemLock's dealloc
# sem_close()s its handle, here the original's.  One small object per unnamed
# recursive lock made after patch().
_shared_handle_twins = []


def _semlock_twin(c):
    """A SEMAPHORE-kind (maxvalue 1) C SemLock on the same kernel semaphore as
    the recursive C SemLock `c`: sem_trywait / sem_post with no owner check."""
    import _multiprocessing
    if c.name is not None:
        try:
            # Its own sem_open of the name: an independent handle that it may
            # close (glibc refcounts the mapping; macOS returns a new handle).
            return _multiprocessing.SemLock._rebuild(
                c.handle, _SEMLOCK_SEMAPHORE, 1, c.name)
        except OSError:
            pass                        # unlinked meanwhile: share the handle
    twin = _multiprocessing.SemLock._rebuild(
        c.handle, _SEMLOCK_SEMAPHORE, 1, None)
    _shared_handle_twins.append(twin)
    return twin


class _CoRecursiveSemLock(object):
    """The `_semlock` of a recursive multiprocessing SemLock (mp.RLock,
    Value/Array.get_lock(), mp.Condition's default lock) under monkey.patch.

    The C RECURSIVE_MUTEX object keys its owner and recursion count on the OS
    thread (semaphore.c ISMINE).  Many fibers share one hub thread, so it let
    a sibling fiber re-enter a section another fiber held; and a fiber that
    parked holding it and resumed on another hub was a stranger to it: its
    re-acquire waited on itself and its release raised
    (docs/dev/FT_CPYTHON_LOCK_GAPS.md, finding 5).  So a fiber never calls that
    object.  Owner and recursion live here, keyed by the fiber, and the
    cross-process gate is a SEMAPHORE-kind twin on the same kernel semaphore
    (sem_trywait / sem_post, no owner check), which works from whichever hub
    the fiber is on.  A plain OS thread keeps the C object's own recursive
    path; the kernel semaphore keeps the two kinds of holder out of each other.
    _is_mine() / _count() answer for a fiber owner from here, so
    mp.Condition.wait (it releases _count() times) and repr() work; every other
    attribute (handle, kind, name, _get_value, ...) is the C object's."""

    __slots__ = ("_c", "_twin", "_owner", "_depth", "__weakref__")

    def __init__(self, c):
        self._c = c
        self._twin = _semlock_twin(c)
        self._owner = None          # the owning fiber (stackweave.current())
        self._depth = 0             # its recursion depth

    def __getattr__(self, name):
        return getattr(self._c, name)

    def _try_take(self):
        if self._twin.acquire(False):              # sem_trywait
            self._owner = stackweave.current()
            self._depth = 1
            return True
        return False

    def acquire(self, block=True, timeout=None):
        if not _in_fiber():
            return self._c.acquire(block, timeout)
        owner = self._owner
        if owner is not None and owner == stackweave.current():
            # Re-entrant: deepen, blocking or not (stdlib RLock bumps the count
            # on a non-blocking re-entrant acquire too).
            self._depth += 1
            return True
        if not block:
            return self._try_take()
        return _semlock_backoff_acquire(self._try_take, timeout)

    def release(self):
        owner = self._owner
        if not _in_fiber() or owner is None or owner != stackweave.current():
            # Not a fiber that holds it: the C object answers, as stock does
            # (it raises for a caller that does not hold it).
            return self._c.release()
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._twin.release()                   # sem_post

    def _is_mine(self):
        if _in_fiber():
            owner = self._owner
            return owner is not None and owner == stackweave.current()
        return self._c._is_mine()

    def _count(self):
        # The holder's recursion depth, whoever asks (as the C _count()).
        if self._owner is not None:
            return self._depth
        return self._c._count()

    def _after_fork(self):
        self._c._after_fork()
        self._twin._after_fork()
        self._owner = None
        self._depth = 0

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        return self.release()


def _patched_semlock_make_methods(self):
    if isinstance(self._semlock, _CoRecursiveSemLock):
        self._semlock = self._semlock._c
    type(self)._stackweave_orig_make_methods(self)   # binds the C acquire/release
    if getattr(self._semlock, "kind", None) == _SEMLOCK_RECURSIVE:
        # RLock (Value/Array.get_lock()): owner and recursion keyed by fiber.
        self._semlock = _CoRecursiveSemLock(self._semlock)
        self.acquire = self._semlock.acquire
        self.release = self._semlock.release
    else:
        # Non-recursive (Lock/Semaphore/Event/Condition): every acquire goes to
        # the real sem, no `_is_mine()` shortcut exists, so only acquire needs
        # the cooperative backoff; release stays the C sem_post.
        self.acquire = _co_semlock_acquire(self._semlock)


# SemLock.__enter__/__exit__ call the C object directly, so `with lock:`
# skipped the cooperative acquire/release (a blocking sem_wait on the hub, and
# for an RLock a hold the cooperative release never saw).  Route them through
# the instance's acquire/release, which for a SemLock made before patch() are
# still the C ones.
def _semlock_enter(self):
    return self.acquire()


def _semlock_exit(self, *args):
    return self.release()


def _patch_mp_connection():
    global _orig_mp_recv_defaults, _orig_mp_send_defaults, _orig_mp_close_defaults
    conn = sys.modules.get("multiprocessing.connection")
    if conn is None:
        return
    Connection = getattr(conn, "Connection", None)
    if Connection is None:
        return
    recv  = Connection.__dict__.get("_recv")
    send  = Connection.__dict__.get("_send")
    close = Connection.__dict__.get("_close")
    # Only the POSIX os.read/os.write/os.close-based Connection has these as
    # plain functions with a captured default; skip otherwise.
    if recv is not None and getattr(recv, "__defaults__", None):
        _orig_mp_recv_defaults = recv.__defaults__
        recv.__defaults__ = (os.read,)
    if send is not None and getattr(send, "__defaults__", None):
        _orig_mp_send_defaults = send.__defaults__
        send.__defaults__ = (os.write,)
    # Rebind _close only when its captured default is the real os.close (the
    # POSIX variant); the win/socket variants capture a different closer.
    if close is not None and getattr(close, "__defaults__", None) == (_raw_os_close,):
        _orig_mp_close_defaults = close.__defaults__
        close.__defaults__ = (os.close,)
    if hasattr(conn, "_read"):
        conn._read = os.read
    if hasattr(conn, "_write"):
        conn._write = os.write


def _install_semlock_patch(sync):
    """Patch sync.SemLock, once per class: a reloaded or re-imported
    multiprocessing.synchronize brings a new class, which gets patched too."""
    SemLock = getattr(sync, "SemLock", None)
    if SemLock is None or not hasattr(SemLock, "_make_methods"):
        return
    if "_stackweave_orig_make_methods" in SemLock.__dict__:
        return
    orig = SemLock._make_methods
    _patched_semlock_classes.append((SemLock, orig,
                                     SemLock.__dict__.get("__enter__"),
                                     SemLock.__dict__.get("__exit__")))
    SemLock._stackweave_orig_make_methods = orig
    SemLock._make_methods = _patched_semlock_make_methods
    SemLock.__enter__ = _semlock_enter
    SemLock.__exit__ = _semlock_exit


class _PatchAfterExecLoader(object):
    """Wraps multiprocessing.synchronize's loader: run the module, then patch
    its SemLock.  Everything else is the real loader's."""

    def __init__(self, loader):
        self._loader = loader

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        _install_semlock_patch(module)

    def __getattr__(self, name):
        return getattr(self._loader, name)


class _SynchronizeImportHook(object):
    """sys.meta_path entry from patch() to unpatch(): whenever
    multiprocessing.synchronize is loaded -- first imported, re-imported after
    leaving sys.modules, or reloaded -- patch its SemLock as it loads.  Without
    it a program that imports multiprocessing after patch() -- the usual order
    -- got stock SemLocks: a blocking sem_wait on the hub, and recursion keyed
    on the hub's OS thread.  Every other import passes by on one string
    compare.  It stays put rather than removing itself: sys.meta_path is a
    plain list that other threads may be walking for their own imports."""

    _NAME = "multiprocessing.synchronize"

    def find_spec(self, name, path=None, target=None):
        if name != self._NAME:
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            find_spec = getattr(finder, "find_spec", None)
            if find_spec is None:
                continue
            spec = find_spec(name, path, target)
            if spec is not None:
                break
        else:
            return None
        if spec.loader is not None and hasattr(spec.loader, "exec_module"):
            spec.loader = _PatchAfterExecLoader(spec.loader)
        return spec


_synchronize_hook = None


def _patch_mp_synchronize():
    # SemLock._make_methods binds self.acquire to the C sem_wait; replace it
    # with a cooperative version.  Only SemLock instances created after this
    # runs cooperate (patch early).  Import synchronize now only when the
    # program already uses multiprocessing, so non-mp users pay nothing; the
    # hook patches every later load of it.
    global _synchronize_hook
    if _synchronize_hook is None:
        _synchronize_hook = _SynchronizeImportHook()
        sys.meta_path.insert(0, _synchronize_hook)
    sync = sys.modules.get("multiprocessing.synchronize")
    if sync is None and "multiprocessing" in sys.modules:
        try:
            import multiprocessing.synchronize as sync
        except ImportError:
            return
    if sync is not None:
        _install_semlock_patch(sync)


def _patch_multiprocessing():
    _patch_mp_connection()
    _patch_mp_synchronize()


def _unpatch_multiprocessing():
    global _synchronize_hook
    if _synchronize_hook is not None:
        try:
            sys.meta_path.remove(_synchronize_hook)
        except ValueError:
            pass
        _synchronize_hook = None
    while _patched_semlock_classes:
        SemLock, make_methods, enter, exit_ = _patched_semlock_classes.pop()
        SemLock._make_methods = make_methods
        for attr, orig in (("__enter__", enter), ("__exit__", exit_)):
            if orig is not None:
                setattr(SemLock, attr, orig)
            else:
                try:
                    delattr(SemLock, attr)
                except AttributeError:
                    pass
        try:
            del SemLock._stackweave_orig_make_methods
        except AttributeError:
            pass
    conn = sys.modules.get("multiprocessing.connection")
    if conn is None:
        return
    Connection = getattr(conn, "Connection", None)
    if Connection is None:
        return
    recv  = Connection.__dict__.get("_recv")
    send  = Connection.__dict__.get("_send")
    close = Connection.__dict__.get("_close")
    if recv is not None and _orig_mp_recv_defaults is not None:
        recv.__defaults__ = _orig_mp_recv_defaults
    if send is not None and _orig_mp_send_defaults is not None:
        send.__defaults__ = _orig_mp_send_defaults
    if close is not None and _orig_mp_close_defaults is not None:
        close.__defaults__ = _orig_mp_close_defaults
    if hasattr(conn, "_read"):
        conn._read = osio._orig_os_read if osio._orig_os_read is not None else os.read
    if hasattr(conn, "_write"):
        conn._write = osio._orig_os_write if osio._orig_os_write is not None else os.write
