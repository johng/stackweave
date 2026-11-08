"""asyncio: a stock event loop run in a fiber keeps its identity across hubs."""
from ._base import *  # noqa: F401,F403  (shared foundation)

# ============================================================
# asyncio -- BaseEventLoop's debug-mode thread check, keyed by fiber
#
# In debug mode (asyncio.run(debug=True), -X dev, PYTHONASYNCIODEBUG,
# IsolatedAsyncioTestCase) BaseEventLoop._check_thread compares
# threading.get_ident() with the _thread_id run_forever recorded, on every
# call_soon / call_at.  A stock loop run in an M:N fiber parks in its (patched)
# selector and may resume on another hub, whose OS thread has another ident:
# the next call_soon from one of its own callbacks raised "Non-thread-safe
# operation invoked on an event loop other than the current one", the callback
# was lost and the loop waited forever (docs/dev/FT_CPYTHON_LOCK_GAPS.md,
# finding 11).  The thread of control a loop belongs to is the fiber that runs
# it, so a loop started in a fiber records that fiber and the check compares
# fibers; _thread_id itself is left as stock sets it (is_running() reads it).
# A loop started off a fiber keeps the stock check.  Nothing else in asyncio
# keys on the OS thread: the running loop and the current task live on the
# thread state, which is per fiber, and call_soon_threadsafe checks nothing.
# stackweave.aio's loop is not a BaseEventLoop and is not touched.
# ============================================================
_orig_run_forever_setup = None
_orig_run_forever_cleanup = None
_orig_check_thread = None


def _patched_run_forever_setup(self):
    _orig_run_forever_setup(self)
    self._stackweave_fiber = stackweave.current() if _in_fiber() else None


def _patched_run_forever_cleanup(self):
    _orig_run_forever_cleanup(self)
    self._stackweave_fiber = None


def _patched_check_thread(self):
    owner = getattr(self, "_stackweave_fiber", None)
    if owner is None or self._thread_id is None:
        return _orig_check_thread(self)
    if _in_fiber() and stackweave.current() == owner:
        return
    raise RuntimeError(
        "Non-thread-safe operation invoked on an event loop other "
        "than the current one")


def _patch_asyncio():
    global _orig_run_forever_setup, _orig_run_forever_cleanup
    global _orig_check_thread
    if _orig_check_thread is not None:
        return
    import asyncio.base_events as be
    loop_cls = be.BaseEventLoop
    if not (hasattr(loop_cls, "_run_forever_setup") and
            hasattr(loop_cls, "_run_forever_cleanup") and
            hasattr(loop_cls, "_check_thread")):
        return
    _orig_run_forever_setup = loop_cls._run_forever_setup
    _orig_run_forever_cleanup = loop_cls._run_forever_cleanup
    _orig_check_thread = loop_cls._check_thread
    loop_cls._run_forever_setup = _patched_run_forever_setup
    loop_cls._run_forever_cleanup = _patched_run_forever_cleanup
    loop_cls._check_thread = _patched_check_thread


def _unpatch_asyncio():
    global _orig_run_forever_setup, _orig_run_forever_cleanup
    global _orig_check_thread
    if _orig_check_thread is None:
        return
    import asyncio.base_events as be
    loop_cls = be.BaseEventLoop
    loop_cls._run_forever_setup = _orig_run_forever_setup
    loop_cls._run_forever_cleanup = _orig_run_forever_cleanup
    loop_cls._check_thread = _orig_check_thread
    _orig_run_forever_setup = _orig_run_forever_cleanup = None
    _orig_check_thread = None
