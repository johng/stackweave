"""Module import locks that know about fibers.

importlib runs a module's code under a per-module lock
(importlib._bootstrap._ModuleLock).  The first importer runs the code; any
other importer of that module waits until it is done and then gets the whole
module; the importer that holds the lock may take it again (a circular import
in one thread), and gets the module as far as it has run.  The lock asks
`_thread.get_ident()` who is importing.  Fibers on one hub share that OS
thread, and a fiber that moves hubs changes it, so:

  * a second fiber on the importer's hub took the lock as its holder instead
    of waiting, and got the half-built module;
  * an importer that parked and resumed on another hub could not release the
    lock ("cannot release un-acquired lock"), and every later importer of that
    module hung (big_100 p527).

install() makes importlib create its module locks as _FiberModuleLock: the
same code (_ModuleLock's own methods), reading a `_thread` whose get_ident()
names the running fiber (the OS thread outside fibers) and whose locks are the
cooperative CoRLock/CoLock.  The locks must be cooperative too: a fiber
waiting for a module on a real lock blocks its hub's OS thread, and an
importer parked on that hub can then never run again to finish.  Only the
locks see that `_thread`, and a lock made before install() keeps the OS
thread for good: an import in flight across install() -- the first
`import stackweave`, made in a fiber -- must release its lock as the identity
that took it.

Fibers can wait where threads could not.  The thread under a run(1) drain,
the thread blocked in mn_run() and the hub fiber under a nested run(1) or Coro
cannot run again until the fibers above them return.  Keyed on the OS thread,
a fiber took the lock of a module one of them was importing as its holder; now
it would wait for good.  So _FiberModuleLock calls that wait a deadlock, which
is how importlib already treats a cycle between two threads: `import x` gets
the module as far as it has run, and importlib.import_module("x") raises
importlib's _DeadlockError.
"""
import _thread
import types

import importlib._bootstrap as _bootstrap
import stackweave_c

from .monkey.locks import CoLock, CoRLock

_os_get_ident = _thread.get_ident
_current_g = stackweave_c.current_g
_mn_current_hub = stackweave_c.mn_current_hub
_mn_run_thread = stackweave_c._mn_run_thread
_nest_owner = stackweave_c._nest_owner


def get_ident():
    """Who is importing: the running fiber, or the OS thread outside one."""
    g = _current_g()
    return _os_get_ident() if g is None else g


class _FiberThread(types.ModuleType):
    """The module locks' `_thread`: the real one, but for get_ident and the
    locks."""

    def __getattr__(self, name):
        return getattr(_thread, name)


_fiber_thread = _FiberThread("_thread")
_fiber_thread.get_ident = get_ident
_fiber_thread.RLock = CoRLock
_fiber_thread.allocate_lock = CoLock

# importlib's globals with `_thread` swapped.  The other names the lock's
# methods read (_blocking_on, _has_deadlocked, _BlockingOnManager,
# _DeadlockError) are bound once, at interpreter startup.
_fiber_globals = dict(vars(_bootstrap), _thread=_fiber_thread)


def _reading_fiber_thread(method):
    """`method` of _ModuleLock, run against _fiber_globals."""
    f = types.FunctionType(method.__code__, _fiber_globals, method.__name__,
                           method.__defaults__, method.__closure__)
    f.__qualname__ = method.__qualname__
    f.__doc__ = method.__doc__
    return f


def _cannot_run_until_caller_returns(owner):
    host = _nest_owner()
    if host is not None:
        # A run(1) drain or Coro on a hub fiber, which is suspended under it.
        return owner == host or owner == _mn_run_thread()
    if _current_g() is None:
        return False
    if _mn_current_hub() is not None:
        return owner == _mn_run_thread()
    # A run(1) drain runs on top of the thread that called it.
    return owner == _os_get_ident()


class _FiberModuleLock(_bootstrap._ModuleLock):
    __init__ = _reading_fiber_thread(_bootstrap._ModuleLock.__init__)
    acquire = _reading_fiber_thread(_bootstrap._ModuleLock.acquire)
    release = _reading_fiber_thread(_bootstrap._ModuleLock.release)
    _importlib_has_deadlock = _reading_fiber_thread(
        _bootstrap._ModuleLock.has_deadlock)

    def has_deadlock(self):
        return (_cannot_run_until_caller_returns(self.owner)
                or self._importlib_has_deadlock())


def install():
    _bootstrap._ModuleLock = _FiberModuleLock
