"""stackweave -- Go-style coroutines in Python.

Everyday API -- `import stackweave` is all you need:
    stackweave.fiber(fn, *args, **kw)   spawn a fiber
    stackweave.run(n, main_fn)       THE entry point. run main_fn with n hubs:
                                  n=1 single-thread, n>1 M:N parallel across n
                                  cores (needs the GIL off; n>1 with the GIL
                                  re-enabled raises).  Collapses mn_init/mn_fiber/
                                  mn_run/mn_fini.  main_fn optional -> drain only.
    stackweave.sleep(seconds)        sleep without blocking the OS thread
    stackweave.yield_now()           cooperative yield (give other fibers a turn)
    stackweave.Chan(capacity=0)      Go-style channel
    stackweave.select(cases, ...)    wait on multiple channel ops
    stackweave.blocking(fn, ...)     offload a blocking/CPU call off the hub
    stackweave.mn_init/mn_fiber/mn_run/mn_fini   raw M:N scheduler (run() wraps these)
    stackweave.backend() / .netpoll_backend()

Feature packages (import as needed, Go-style):
    stackweave.monkey  -- make blocking stdlib cooperative (manual: .patch())
    stackweave.time    -- After / Tick / Timer / Ticker
    stackweave.context -- Background / WithCancel / WithTimeout
    stackweave.sync    -- blocking-style sockets + Lock/Event/Semaphore
    stackweave.aio     -- run async/await code on the scheduler

The raw C extension stays importable as `stackweave_c` for advanced use
(TCPConn, wait_fd, warmup, raw Coro/G handles, fiber_noyield, ...).
"""
# Distribution version, read from the installed package metadata so that
# pyproject.toml stays the single source of truth.
from importlib.metadata import version, PackageNotFoundError
try:
    __version__ = version("stackweave")
except PackageNotFoundError:        # running from an uninstalled source tree
    __version__ = "0.2.0"
del version, PackageNotFoundError

import sys as _sys

# CPython's per-thread recursion counter is not swapped across our
# ucontext stack switch (v0 -- properly handled in the M:N C path
# planned for phase 3).  Each stackweave.yield_now() permanently decrements the
# counter on the OS thread, so a long stackweave.run() eventually hits
# RecursionError.  Bumping the limit makes the leak tolerable for
# anything short of a multi-hour service; the proper fix is to
# save/restore tstate->py_recursion_remaining + c_recursion_remaining
# in the C resume/yield path.
if _sys.getrecursionlimit() < 1_000_000:
    _sys.setrecursionlimit(1_000_000)

from .runtime import (
    fiber,
    fiber_fast,    # experimental C fast-spawn entry (bypasses grow-down; under optimization)
    yield_now,
    yield_,        # deprecated alias for yield_now (keyword-dodge name)
    sleep,
    blocking,
    run,
    current,
    Goroutine,
    set_grow_down,      # toggle the default-on function-bound stack grow-down
    grow_down_enabled,
)
import stackweave_c as _core  # noqa: F401  – C extension lives at top level

backend = _core.backend
netpoll_backend = _core.netpoll_backend

# Re-export the core scheduler + channel primitives so `import stackweave` is all
# everyday code needs -- no separate `import stackweave_c`.  (go / run / sleep /
# yield_now / blocking / current above are the friendly wrappers from .runtime.)
Chan = _core.Chan
select = _core.select

# M:N scheduler -- real multi-core parallelism on free-threaded CPython.
# Everyday code uses run(n, main_fn); these raw entry points stay exposed for
# advanced use (custom spawn loops, benchmarks).  mn_hub_count() reports how
# many hubs are live and is what the stackweave.fiber() wrapper dispatches on.
mn_init = _core.mn_init
mn_fiber = _core.mn_fiber
mn_run = _core.mn_run
mn_fini = _core.mn_fini
mn_hub_count = _core.mn_hub_count
mn_hub_states = _core.mn_hub_states   # per-hub diagnostic snapshot (see inspect.hubs)

# Lower-level C primitives, surfaced here so `stackweave_c` never needs importing
# directly for normal use.  (The raw module stays available as `stackweave_c` for
# deep internals: sched_*, park_self, the get_/set_ tuning knobs, etc.)
TCPConn = _core.TCPConn                # C-level TCP connection (Go-parity perf)
Coro = _core.Coro                      # raw stackful coroutine handle
G = _core.G                            # fiber handle type
wait_fd = _core.wait_fd                # park the current fiber on fd readiness
WAIT_FD_CANCELLED = _core.WAIT_FD_CANCELLED
tcp_recv = _core.tcp_recv
tcp_send = _core.tcp_send
fiber_noyield = _core.fiber_noyield          # faster spawn for run-to-completion work
warmup = _core.warmup                  # pre-allocate the per-thread stack pool
prewarm = _core.prewarm                # opt-in: pre-fill the GLOBAL stack depot
                                       # (call it yourself; never on by default)
prewarm_keep = _core.prewarm_keep      # opt-in: CONTINUOUS background top-up daemon
prewarm_stop = _core.prewarm_stop      # stop the continuous daemon
thread_init = _core.thread_init
thread_fini = _core.thread_fini
preempt_init = _core.preempt_init
preempt_fini = _core.preempt_fini
iouring_available = _core.iouring_available

# Fork safety: after os.fork() the child keeps only the forking thread, so the
# M:N hub threads and the blocking-offload workers are gone.  Reset the C
# runtime in the child so it neither hangs (stackweave_c.run / mn_run waiting on
# dead hubs) nor deadlocks on a lock a dead thread held at fork, and so the
# child gets its own netpoll fd instead of sharing the parent's.  Registered
# here once, at import, for ALL stackweave use (the monkey layer adds its own,
# higher-level child handler on top).
import os as _os
if hasattr(_os, "register_at_fork"):
    _os.register_at_fork(after_in_child=_core.reset_after_fork)

# Opt-in crash reporter: set STACKWEAVE_CRASH (on/all/wait/gdb/backtrace/pystack)
# to install a fatal-signal handler at import, so a SIGSEGV (e.g. a fiber
# stack overflow) prints a classified fiber dump instead of dying silently.
# Off by default -- we don't hijack process-wide signal handlers unless asked.
# Installed here, before the runtime starts, so the scheduler hubs are armed as
# they spawn.  See stackweave.inspect.install_crash_handler() to install in code.
if _os.environ.get("STACKWEAVE_CRASH", "").strip().lower() not in ("", "0", "off"):
    try:
        _core.install_crash_handler(
            _os.environ.get("STACKWEAVE_CRASH"),
            _os.environ.get("STACKWEAVE_CRASH_FILE"),
        )
    except Exception:   # never let crash-reporter setup break import
        pass

# Opt-in adaptive stack auto-sizer: STACKWEAVE_STACK_AUTOSIZE=1 starts each
# fiber kind large and learns its real size down over its first runs (in
# memory only, never persisted).  Off by default -- it changes per-kind stack
# sizes.  See stackweave.inspect.enable_stack_autosize().
_autosize_env = _os.environ.get("STACKWEAVE_STACK_AUTOSIZE", "").strip().lower()
if _autosize_env in ("1", "on", "true", "prescan"):
    try:
        _core.enable_stack_autosize(True, _autosize_env == "prescan")
    except Exception:
        pass

# ---- Cross-hub fiber migration (opt-in, behind flags) -----------------------
# A parked fiber normally resumes on the SAME hub it parked on.  With migration
# enabled, a woken fiber is routed to a global run-queue and resumed on ANY idle
# hub -- so work stranded behind a wedged hub gets rescued and load spreads to
# free cores.  This needs each fiber to own a migratable PyThreadState (per-g
# tstate), which is only HEAP-SAFE when CPython is built with the optional
# alloc-home patch (src/patches/cpython31Xt-tstate-alloc-home.patch): the per-g
# tstate then borrows the running hub's allocator, so no per-fiber heap migrates
# OS threads.  Off by default; turn on with STACKWEAVE_MIGRATION=1 in the
# environment, or stackweave.enable_migration(), BEFORE the runtime starts (the
# flag is read once at mn_init).
def migration_available():
    """True iff this build can SAFELY migrate fibers across hubs -- i.e. it was
    compiled against BOTH optional CPython patches (see src/patches/):

      alloc-home (Py_TSTATE_ALLOC_HOME) -- a migrated fiber allocates on the hub
        now running it, so no per-fiber heap migrates OS threads.
      exec-home  (Py_TSTATE_EXEC_HOME)  -- _PyThreadState_GET() and _Py_ThreadId()
        stay non-cacheable, so a resumed fiber can't keep using the origin hub's
        thread state or resolve biased-refcount ownership against a stale thread
        id (a use-after-free).

    Either patch alone is insufficient.  When this is False, migration is only
    reachable via the STACKWEAVE_ALLOW_UNSAFE_MIGRATION dev override (which can
    crash under churn).  See migration_status() for which half is missing.

    NOTE exec-home is a codegen property and _Py_ThreadId() inlines into
    Py_INCREF/Py_DECREF via the public refcount.h, so this can only speak for
    stackweave's own extension.  A fully sound migrating process also needs every
    OTHER extension module rebuilt against the patched interpreter."""
    return bool(getattr(_core, "alloc_home_available", 0)) and \
           bool(getattr(_core, "exec_home_available", 0))

def migration_status():
    """Which halves of the migration safety gate this build has, as a dict:
    {"alloc_home": bool, "exec_home": bool, "available": bool}.  Useful for
    diagnosing a migration_available() of False."""
    alloc = bool(getattr(_core, "alloc_home_available", 0))
    exech = bool(getattr(_core, "exec_home_available", 0))
    return {"alloc_home": alloc, "exec_home": exech, "available": alloc and exech}

def migration_enabled():
    """True iff cross-hub migration is REQUESTED for the next runtime start
    (STACKWEAVE_MIGRATION / STACKWEAVE_PER_G_TSTATE / STACKWEAVE_STEAL_WOKEN set in the
    environment).  Whether it actually activates also depends on
    migration_available(); on stock CPython without the unsafe override the
    scheduler warns and falls back to the default (non-migrating) mode."""
    return any(
        _os.environ.get(v, "").strip() not in ("", "0")
        for v in ("STACKWEAVE_MIGRATION", "STACKWEAVE_PER_G_TSTATE", "STACKWEAVE_STEAL_WOKEN")
    )

def enable_migration(allow_unsafe=False):
    """Opt into cross-hub fiber migration.  Must be called BEFORE the M:N runtime
    starts (run() / mn_init); the flag is read once at init.  On a build WITHOUT
    the alloc-home patch this raises RuntimeError unless allow_unsafe=True, which
    also sets the STACKWEAVE_ALLOW_UNSAFE_MIGRATION dev escape hatch (per-g tstate
    migration can then crash under churn at >1 hub -- dev/fuzzing only).
    Idempotent."""
    if not migration_available() and not allow_unsafe:
        st = migration_status()
        # The patches are version-specific: name the one for the running interpreter.
        _tag = "cpython%d%dt" % _sys.version_info[:2]
        missing = ", ".join(
            name for name, have in (
                ("alloc-home (src/patches/%s-tstate-alloc-home.patch)" % _tag,
                 st["alloc_home"]),
                ("exec-home (src/patches/%s-tstate-exec-home.patch)" % _tag,
                 st["exec_home"]),
            ) if not have
        )
        raise RuntimeError(
            "stackweave: cross-hub migration needs CPython built with BOTH optional "
            f"patches; missing: {missing}. Without alloc-home a per-g "
            "PyThreadState's heap migrates across hub threads and crashes under "
            "churn; without exec-home the compiler caches the thread-identity "
            "reads across a park/resume, so a migrated fiber uses the origin "
            "hub's thread state and mis-resolves biased-refcount ownership "
            "(use-after-free). Rebuild CPython against the patches (and rebuild "
            "extension modules against it), or pass allow_unsafe=True for "
            "dev/fuzzing."
        )
    _os.environ["STACKWEAVE_MIGRATION"] = "1"
    if allow_unsafe and not migration_available():
        _os.environ["STACKWEAVE_ALLOW_UNSAFE_MIGRATION"] = "1"

# Runtime introspection -- `stackweave.inspect.dump()`, fibers(), stack(), etc.
# See stackweave/inspect.py.  Exposed as a submodule plus a couple of top-level
# conveniences (the common "what are all my fibers doing" calls).
from . import inspect  # noqa: E402,F401
fibers = inspect.fibers
dump = inspect.dump
hubs = inspect.hubs

from ._optimize import optimize  # noqa: E402,F401  – one call, named trade-offs
from ._hot import hot  # noqa: E402,F401  – mark a hot handler for per-core scaling

# Feature packages, imported eagerly so that `import stackweave` is the ONLY
# import statement you ever need.  Importing them has no side effects -- in
# particular monkey patches the stdlib only when you call stackweave.monkey.patch().
# (monkey first: stackweave.sync builds its Lock/Event on monkey's primitives.)
from . import monkey   # noqa: E402,F401  – cooperative stdlib (manual .patch())
from . import time     # noqa: E402,F401  – After / Tick / Timer / Ticker
from . import context  # noqa: E402,F401  – Background / WithCancel / WithTimeout
from . import sync     # noqa: E402,F401  – blocking-style sockets + sync prims
from .sync import WaitGroup, Future, gather  # noqa: E402,F401  – fan-in primitives
from . import aio      # noqa: E402,F401  – run async/await on the scheduler
from .stats import stats  # noqa: E402,F401  – R0 process-wide gauge surface

__all__ = [
    # scheduler
    "fiber", "fiber_fast", "run", "sleep", "yield_now", "yield_", "blocking", "current",
    "Goroutine", "fiber_noyield", "warmup", "prewarm", "prewarm_keep", "prewarm_stop",
    "optimize", "hot",
    "thread_init", "thread_fini",
    "preempt_init", "preempt_fini",
    # channels
    "Chan", "select",
    # fan-in primitives
    "WaitGroup", "Future", "gather",
    # M:N
    "mn_init", "mn_fiber", "mn_run", "mn_fini", "mn_hub_count", "mn_hub_states",
    "hubs",
    # cross-hub migration (opt-in, needs the alloc-home CPython patch)
    "migration_available", "migration_status", "migration_enabled",
    "enable_migration",
    # low-level I/O primitives
    "TCPConn", "Coro", "G", "wait_fd", "WAIT_FD_CANCELLED",
    "tcp_recv", "tcp_send", "iouring_available",
    # introspection
    "backend", "netpoll_backend", "fibers", "dump", "inspect", "stats",
    # feature packages
    "monkey", "time", "context", "sync", "aio",
    "__version__",
]
