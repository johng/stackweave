"""Regression guard for the parked-fiber-frame GC visibility fix.

Free-threaded CPython credits PEP-703 deferred-refcount stackrefs (code objects,
functions, deferred locals) held on a thread's stack ONLY by walking live
tstates' current_frame chains.  A PARKED stackweave fiber's suspended frames live in
g->snap, invisible to that walk, so with the specializing interpreter on (TLBC)
their deferred-only referents were freed early and re-used after resume -> heap
corruption -> SIGSEGV (big_100 p565/p524).  stackweave_c ships the fix: a GC-tracked
"frames anchor" (module_gcframes.c.inc) that, under stop-the-world, walks the
fiber registry and visits every parked chain so those referents are credited.

The definitive oracle is p565/p524 themselves: they PASS with the anchor on and
used to SIGSEGV without it.  The anchor is now unconditional, so the p565 PASS
arm here is the regression guard.

A NOTE on why there is no pure-Python weakref oracle: constructing a
deferred-ONLY-referenced object reliably needs the many-fiber, code-object churn
p565 provides.  A lone parked fiber's frames often stay on the idle hub tstate
(so CPython's own walk credits them), and raw-Coro / dict-loaded callables arrive
as STRONG references (in the refcount, never freed regardless of frame
visibility).  So p565 is the trustworthy teeth; the in-process arms below check
the wiring (anchor active, freeze callback, self-consistency).
"""
import gc
import os
import subprocess
import sys

import pytest

import stackweave  # noqa: F401  (ensures the extension + interlock are importable)
import stackweave_c as rc

_FT = not sys._is_gil_enabled()
pytestmark = pytest.mark.skipif(
    not _FT, reason="parked-frame GC visibility fix is free-threaded 3.14+ only")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")


def test_anchor_active_by_default():
    # The interlock (stackweave.runtime._tlbc_reexec_if_needed) keeps TLBC on only
    # while this is 1; it must be active out of the box on FT 3.14+.
    assert rc.gc_frames_active == 1


def test_anchor_is_gc_tracked_and_collect_is_clean():
    assert gc.is_tracked(rc.gc_frames_anchor)
    gc.collect()
    assert rc._self_check(0) == 0


def test_gc_freeze_callback_registered():
    # AM-8: a gc "start" callback keeps the anchor thawed across gc.freeze(), so a
    # prefork server's copy-on-write freeze cannot silently reopen the crash.
    names = [getattr(cb, "__name__", "") for cb in gc.callbacks]
    assert any("runloom_gc_frames" in n for n in names)


def test_gc_freeze_keeps_anchor_thawed_and_collect_clean():
    # Freeze the whole heap (as gunicorn --preload would), then a collection must
    # still run cleanly with the anchor participating (the callback thaws it).
    try:
        gc.collect()
        gc.freeze()
        gc.collect()
        assert rc._self_check(0) == 0
    finally:
        gc.unfreeze()


def _run_big100(prog, extra_env, hubs, duration, timeout):
    env = dict(os.environ, PYTHON_GIL="0", PYTHONPATH=_SRC)
    env.update(extra_env)
    p = subprocess.run(
        [sys.executable, os.path.join(_REPO, "tests", "big_100", prog),
         "--hubs", str(hubs), "--duration", str(duration)],
        env=env, cwd=_REPO, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def test_p565_passes_tlbc_on_with_anchor():
    # The routine regression guard: with TLBC on and the anchor active, the
    # compileall churn that used to crash now runs clean.  If the anchor ever
    # regresses, this SIGSEGVs (rc != 0) within ~2s.
    rc0, out = _run_big100(
        "p565_compileall_bytecode_purity.py", {},
        hubs=8, duration=8, timeout=90)
    assert rc0 == 0 and "VERDICT       : PASS" in out, out[-2500:]
