"""Per-test invariant checks for the stackweave suite.

Every test in this directory runs through an autouse fixture that, AFTER the
test body, asserts two things about the C runtime:

  1. ``stackweave_c._self_check(0) == 0`` -- a structural walk of every live
     scheduler / netpoll data structure (no list cycle, no self-looping
     per-fd bucket, the atomic parked count matches the walked count, no
     bucket entry missing from the global list).  This is a pure consistency
     invariant: it holds regardless of how much work a test left behind, so
     it never false-positives on a legitimately-busy background loop thread.

  2. No *leaked* netpoll parker.  We snapshot ``stats()['netpoll_parked']``
     before the test and re-read it after; a fiber that parked in
     ``wait_fd`` and never got woken (the cross-thread-drain / leaked-parker
     class of bug) shows up as a count that never settles back.  A short
     settle window absorbs teardown races where a background thread is about
     to drain its own parker.

Why this exists: in practice a leaked parker did not fail the test that
caused it -- it wedged an *unrelated* ``stackweave_c.run()`` several files later,
which is brutal to bisect.  Attributing the leak to the test that created it
(via a per-test before/after delta) turns "the suite hangs sometimes" into
"this one test leaked a parker."

Opt out with ``@pytest.mark.runloom_leaky`` for a test that deliberately leaves a
parker behind (e.g. the regression that proves a leaked parker no longer
wedges other threads).

Env knobs:
  STACKWEAVE_TEST_LEAK_REPORT=1  -- print the per-test parked delta instead of
                              failing on it (survey mode; self_check still
                              hard-asserts).
  STACKWEAVE_TEST_NO_INVARIANTS=1 -- disable the fixture entirely.
"""
import os
import sys
import time

# Match run_tests.py / test_mn.py: test the in-tree .so, not whatever else
# might be on the path.  Harmless if stackweave_c is already imported (Python
# caches the module, so the fixture inspects the same runtime the tests use).
_TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_TESTS)
_SRC = os.path.join(REPO, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import threading

import pytest

try:
    import stackweave_c
except Exception:  # pragma: no cover - stackweave_c should always import here
    stackweave_c = None

_REPORT_ONLY = os.environ.get("STACKWEAVE_TEST_LEAK_REPORT") == "1"
_DISABLED = os.environ.get("STACKWEAVE_TEST_NO_INVARIANTS") == "1"

# --- swallowed-error gate (QA-steal-V2 #3) ---------------------------------
# Errors raised on a path that cannot propagate -- a tp_dealloc / weakref
# finalizer that raises, an exception in a callback run on a hub OS thread, an
# unawaited-task error -- go to sys.unraisablehook / threading.excepthook and
# VANISH (in the free-threaded build, concurrently across many hubs), often
# corrupting half-reclaimed state that later surfaces as an unrelated UAF.
# Install a process-wide gate (below) so any such swallowed error fails the test
# it fired under instead of disappearing.  A test that INTENTIONALLY raises on
# such a path opts out with @pytest.mark.runloom_allow_unraisable; tests using
# test.support.catch_unraisable_exception install their own hook for their scope
# and are unaffected.  STACKWEAVE_TEST_LEAK_REPORT=1 makes it report-only too.
_UNRAISABLE = []
_pg_saved_unraisablehook = None
_pg_saved_threadexcepthook = None


def _pg_unraisable_hook(unraisable):
    try:
        _UNRAISABLE.append("unraisable[{0}]: {1!r} (obj {2!r})".format(
            getattr(unraisable, "err_msg", None) or "Exception ignored",
            getattr(unraisable, "exc_value", None),
            getattr(unraisable, "object", None)))
    except Exception:  # never let the hook itself raise
        _UNRAISABLE.append("unraisable: <unformattable>")


def _pg_thread_excepthook(args):
    try:
        _UNRAISABLE.append("thread-excepthook: {0!r} on {1!r}".format(
            getattr(args, "exc_value", None), getattr(args, "thread", None)))
    except Exception:
        _UNRAISABLE.append("thread-excepthook: <unformattable>")

# How long to let a background thread finish draining its own parker before we
# call a non-zero delta a real leak.  Real leaks never drain, so this only
# costs wall-clock on a genuine failure or a slow teardown.
_SETTLE_DEADLINE_S = 0.5
_SETTLE_STEP_S = 0.01


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "runloom_leaky: test deliberately leaves a netpoll parker behind; "
        "skip the post-test parked-leak invariant for it.")
    config.addinivalue_line(
        "markers",
        "runloom_allow_unraisable: test intentionally raises on a dealloc / "
        "finalizer / hub-thread path; skip the swallowed-error gate for it.")
    config.addinivalue_line(
        "markers",
        "known_gap: an xfail from tests/known_gaps.py (added at collection).")
    if not _DISABLED:
        global _pg_saved_unraisablehook, _pg_saved_threadexcepthook
        _pg_saved_unraisablehook = sys.unraisablehook
        _pg_saved_threadexcepthook = threading.excepthook
        sys.unraisablehook = _pg_unraisable_hook
        threading.excepthook = _pg_thread_excepthook


# ---------------------------------------------------------------------------
# Migration is always on, so the suite only runs where migration is sound.
#
# Every M:N run migrates fibers between hubs, and that is only sound on a
# free-threaded interpreter with the GIL off, built with BOTH src/patches/
# halves, running an extension built with them too (CLAUDE.md, "Build &
# test").  Anywhere else the M:N tests crash under churn, or quietly test
# something other than what ships, so the session stops before collecting
# anything instead.  The interpreter check is the rule of
# stackweave.runtime._interpreter_migration_patched, repeated here so that
# this file does not import the stackweave package before the tests do.
_MIGRATION_FEATURES = ("Py_TSTATE_ALLOC_HOME", "Py_TSTATE_EXEC_HOME")


def _interpreter_migration_patched():
    import sysconfig
    cppflags = (sysconfig.get_config_var("CONFIGURE_CPPFLAGS") or "").split()
    defined = {f[2:].split("=", 1)[0] for f in cppflags if f.startswith("-D")}
    return all(sysconfig.get_config_var(f) or f in defined
               for f in _MIGRATION_FEATURES)


def _migration_problems(runtime_gil=True):
    """What keeps migration from being sound here, as a list of reasons.
    runtime_gil=False skips the check of THIS process's GIL, for a launcher
    (tests/run_isolated.py) whose children set PYTHON_GIL=0 themselves."""
    import sysconfig
    problems = []
    if not sysconfig.get_config_var("Py_GIL_DISABLED"):
        problems.append("%s is not a free-threaded build" % sys.executable)
    elif runtime_gil and sys._is_gil_enabled():
        problems.append("the GIL is enabled (run with PYTHON_GIL=0, and "
                        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 if a plugin turned "
                        "it back on)")
    if not _interpreter_migration_patched():
        problems.append("%s was built without both migration patches "
                        "(src/patches/)" % sys.executable)
    if stackweave_c is not None and not getattr(
            stackweave_c, "migration_patched", 0):
        problems.append("stackweave_c was built without -DPy_TSTATE_ALLOC_HOME "
                        "-DPy_TSTATE_EXEC_HOME (rebuild it with this "
                        "interpreter)")
    return problems


def pytest_sessionstart(session):
    problems = _migration_problems()
    if problems:
        pytest.exit("this suite needs cross-hub migration to be sound, and it "
                    "is not here: " + "; ".join(problems),
                    returncode=pytest.ExitCode.USAGE_ERROR)


# ---------------------------------------------------------------------------
# Known gaps.
#
# Every known gap is marked at its test with a helper from known_gaps.py.  The
# hook below tags each one `known_gap` (so `pytest -m known_gap` lists them)
# and fails collection on an xfail that does not use a helper, so the gaps all
# read the same way and none is a quiet one-off.  The vendored suites in
# subdirectories (tests/aio/, tests/net/) keep their own conventions.
from known_gaps import PREFIXES as _GAP_PREFIXES


def pytest_collection_modifyitems(config, items):
    stray = []
    for item in items:
        marks = list(item.iter_markers("xfail"))
        if not marks:
            continue
        item.add_marker(pytest.mark.known_gap)
        if os.path.dirname(str(item.path)) != _TESTS:
            continue
        for m in marks:
            if not str(m.kwargs.get("reason", "")).startswith(_GAP_PREFIXES):
                stray.append(item.nodeid)
    if stray:
        raise pytest.UsageError(
            "xfail without a tests/known_gaps.py helper (its reason must start "
            "with one of %s): %s" % (", ".join(p.strip() for p in _GAP_PREFIXES),
                                     ", ".join(stray)))


def pytest_unconfigure(config):
    if _pg_saved_unraisablehook is not None:
        sys.unraisablehook = _pg_saved_unraisablehook
    if _pg_saved_threadexcepthook is not None:
        threading.excepthook = _pg_saved_threadexcepthook


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    # Stash each phase's report on the item so the fixture teardown can tell
    # whether the test body itself failed (in which case piling a leak error
    # on top is just noise -- the real failure already explains it).
    rep = yield
    setattr(item, "_pg_rep_" + rep.when, rep)
    return rep


def _parked():
    # Per-sched count (this thread's sched), not the global one: a parker
    # stranded on another/since-exited thread's sched (e.g. a test that
    # deliberately leaks one on a dead thread) is not this test's leak and
    # must not trip the check.  Falls back to the global count on an older .so.
    s = stackweave_c.stats()
    return int(s.get("netpoll_parked_self", s["netpoll_parked"]))


def _settle_parked(baseline):
    """Return the parked count, giving in-flight teardown up to the settle
    deadline to bring it back down to <= baseline."""
    cur = _parked()
    if cur <= baseline:
        return cur
    deadline = time.monotonic() + _SETTLE_DEADLINE_S
    while time.monotonic() < deadline:
        time.sleep(_SETTLE_STEP_S)   # let background loop threads run + drain
        cur = _parked()
        if cur <= baseline:
            break
    return cur


@pytest.fixture(autouse=True)
def runloom_invariants(request):
    if _DISABLED or stackweave_c is None:
        yield
        return

    baseline = _parked()
    del _UNRAISABLE[:]          # count only THIS test's swallowed errors
    yield

    # Don't mask a real test failure with a teardown invariant error.
    call_rep = getattr(request.node, "_pg_rep_call", None)
    if call_rep is not None and not call_rep.passed:
        return

    # (0) swallowed-error gate: an unraisable / thread-excepthook that fired
    # during this test (a raise on a dealloc / finalizer / hub-thread path that
    # cannot propagate) is a real fault, not benign -- surface it here.
    if (_UNRAISABLE
            and request.node.get_closest_marker("runloom_allow_unraisable") is None):
        caught = list(_UNRAISABLE)
        del _UNRAISABLE[:]
        msg = ("{0} error(s) swallowed on a dealloc/finalizer/hub-thread path "
               "during this test (sys.unraisablehook / threading.excepthook): "
               "{1}".format(len(caught), " | ".join(caught[:5])))
        if _REPORT_ONLY:
            sys.stderr.write("[runloom-unraisable] {0}::{1}: {2}\n".format(
                request.node.module.__name__, request.node.name, msg))
        else:
            pytest.fail(msg, pytrace=False)

    # (1) structural integrity -- always holds, cheap, no false positives.
    viol = stackweave_c._self_check(0)
    assert viol == 0, (
        "stackweave_c._self_check reported {0} violation(s) after this test "
        "(see stderr [runloom-diag] lines): netpoll/scheduler structures are "
        "inconsistent.".format(viol))

    # (2) leaked-parker delta.
    if request.node.get_closest_marker("runloom_leaky") is not None:
        return
    after = _settle_parked(baseline)
    delta = after - baseline
    if delta > 0:
        msg = ("leaked {0} netpoll parker(s): netpoll_parked was {1} before "
               "the test and {2} after (did not drain within {3}s). A "
               "fiber parked in wait_fd was never woken -- mark the test "
               "@pytest.mark.runloom_leaky if that is intentional.".format(
                   delta, baseline, after, _SETTLE_DEADLINE_S))
        if _REPORT_ONLY:
            sys.stderr.write("[runloom-leak] {0}::{1}: {2}\n".format(
                request.node.module.__name__, request.node.name, msg))
        else:
            pytest.fail(msg, pytrace=False)
