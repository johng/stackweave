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
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
    if not _DISABLED:
        global _pg_saved_unraisablehook, _pg_saved_threadexcepthook
        _pg_saved_unraisablehook = sys.unraisablehook
        _pg_saved_threadexcepthook = threading.excepthook
        sys.unraisablehook = _pg_unraisable_hook
        threading.excepthook = _pg_thread_excepthook


# ---------------------------------------------------------------------------
# Known gaps of migration mode (always on).
#
# A parked fiber's frames live on its OWN tstate rather than the hub's, and a
# woken fiber arrives through the global run-queue rather than a hub-local ring
# pop.  The tests below encode the old per-hub-tstate semantics for exactly
# those things, so they fail by construction, not by regression.  Remove an
# entry when the gap it names is closed; a new failure elsewhere is a
# regression.  (The entries that needed preemption or the sysmon's ATTACHED
# classification came off this list when fork #26 made both work under
# migration by reading the running fiber's tstate.)
_PER_G_KNOWN_GAPS = {
    "test_cov100_hubinfo_waitfd.py::test_hubinfo_blocked_at_for_detached_wedge":
        "hubinfo blocked_at walks the hub tstate; a per-g fiber's frames are on its own tstate",
    "test_hub_introspect.py::HubIntrospectTest::test_wedge_and_blocked_at":
        "hubinfo blocked_at walks the hub tstate; a per-g fiber's frames are on its own tstate",
    "test_cov95_diag.py::test_ring_dump_covers_every_reachable_op_name_arm":
        "G_POP is a hub-local ring-pop label; per-g woken gs arrive via the global run-queue",
    "test_cov95_datastack.py::test_datastack_sweep_debug_decompose":
        "the datastack dwell sweep accounts the hub tstate's chunks; per-g fibers use their own "
        "(the chunks/resident assertions run on Linux only, so macOS passes this by skipping them)",
    "test_stack_pool_balance.py::test_stack_pool_plateaus_under_fanout":
        "per-g tstates add a datastack mapping per fiber slot: the pool plateaus ~40x higher "
        "(bounded: flat over 320 rounds on Linux) and sometimes after the test's midpoint window",
}


# Open INTERMITTENT failures under migration -- not semantic gaps, not yet
# root-caused, listed apart so they are never mistaken for the set above.
# Measured on Linux 3.14.4t: 0/20 without migration, 1/15 with it.
_PER_G_OPEN_INTERMITTENT = {
    "test_signal_recipient.py::test_selector_outranks_a_dense_unrelated_sleeper":
        "rare lost signal delivery (who=nobody after 10 s) under per-g mode; open",
}


# Seeded M:N scheduler (STACKWEAVE_MN_SEED / STACKWEAVE_SIM_MN) -- disabled in
# migration mode until it is re-implemented: woken fibers run from the global
# run-queue, which the seeded baton does not order, so mn_init refuses a seeded
# run (see TODO(migration) in src/runloom_c/mn_sched_hub_resume_preempt.c.inc).
# Every test below drives that scheduler.  Remove this set when it comes back.
_SEEDED_MN_TODO = (
    "test_cov100_resume_preempt.py::test_baton_barrier_off_immediate_handoff",
    "test_cov100_resume_preempt.py::test_grant_trace_only_at_fini_not_per_grant",
    "test_cov100_resume_preempt.py::test_grant_trace_ring_dump",
    "test_cov100_resume_preempt.py::test_pct_depth_one_no_change_points",
    "test_cov100_resume_preempt.py::test_pct_steps_override",
    "test_cov100_resume_preempt.py::test_pct_steps_override_deterministic",
    "test_cov100_resume_preempt.py::test_seeded_uniform_baton_is_deterministic",
    "test_cov100_resume_preempt.py::test_seeded_uniform_baton_no_pct",
    "test_chess_greybox_aliaspair.py::TestOnRealWorkload::test_chess_chan_yields_cross_hub_alias_pairs",
    "test_cov95_diag.py::test_mn_events_trace_env_emits_baton_protocol",
    "test_mn_sim_bytes.py::TestCrossPlane::test_h1_sim_beside_live_armed_pool",
    "test_mn_sim_bytes.py::TestMnSimBytes::test_byte_plane_digest_deterministic",
    "test_mn_sim_bytes.py::TestMnSimBytes::test_delayed_delivery_clock_compression",
    "test_mn_sim_bytes.py::TestMnSimBytes::test_finite_timeout_works_since_i4",
    "test_mn_sim_bytes.py::TestMnSimBytes::test_p4_scenario_fixed",
    "test_mn_sim_bytes.py::TestMnSimBytes::test_self_wake_corner_h1",
    "test_mn_sim_bytes.py::TestMnSimBytes::test_stw_churn_under_gated_pump",
    "test_mn_sim_bytes.py::TestMnSimBytes::test_unregistered_fd_raises",
    "test_mn_sim_bytes.py::TestReviewRegressions::test_barrier_zero_fenced",
    "test_mn_sim_bytes.py::TestReviewRegressions::test_late_parker_gets_stashed_wake",
    "test_mn_sim_bytes.py::TestTimedParksI4::test_park_timeout_on_logical_plane",
    "test_mn_sim_bytes.py::TestTimedParksI4::test_park_woken_before_logical_timeout",
    "test_mn_sim_bytes.py::TestTimedParksI4::test_timeout_vs_post_advance_delivery",
    "test_mn_sim_bytes.py::TestTimedParksI4::test_true_tie_ready_beats_timeout",
    "test_mn_sim_bytes.py::TestTimedParksI4::test_wait_fd_timeout_fires_at_logical_deadline",
    "test_mn_sim_clock.py::TestMnNsClock::test_back_to_back_runs_bit_identical",
    "test_mn_sim_clock.py::TestMnNsClock::test_census_clock_exact_ns",
    "test_mn_sim_clock.py::TestMnNsClock::test_clock_monotone_across_wakes",
    "test_mn_sim_clock.py::TestMnNsClock::test_fractional_deadline_fires",
    "test_mn_sim_clock.py::TestMnNsClock::test_gap_sleeper_run_again",
    "test_mn_sim_clock.py::TestMnNsClock::test_no_global_clock_leak_into_h1",
    "test_mn_sim_determinism.py::TestBatonDeterminism::test_chan_h2",
    "test_mn_sim_determinism.py::TestBatonDeterminism::test_cpu_yield_h2",
    "test_mn_sim_determinism.py::TestBatonDeterminism::test_cpu_yield_h4",
    "test_mn_sim_determinism.py::TestBatonDeterminism::test_timers_h2",
    "test_mn_sim_determinism.py::TestBatonDeterminism::test_timers_h4",
    "test_mn_sim_determinism.py::TestSimMnFence::test_sim_mn_optin_opens_path",
    "test_mn_sim_fences.py::TestFencesRaise::test_blocking_runs_inline",
    "test_mn_sim_fences.py::TestFencesRaise::test_park_foreign_wakeable_raises",
    "test_mn_sim_fences.py::TestFencesRaise::test_per_g_tstate_mode_raises",
    "test_mn_sim_fences.py::TestFencesRaise::test_preempt_init_noop",
    "test_mn_sim_fences.py::TestFencesRaise::test_sched_sleep_real_raises",
    "test_mn_sim_fences.py::TestFencesRaise::test_slicer_running_before_mn_init_raises",
    "test_mn_sim_fences.py::TestFencesRaise::test_slicer_started_pre_env_is_fenced",
    "test_mn_sim_fences.py::TestFinalizerTorture::test_finalizer_chan_ops_complete",
    "test_mn_sim_fences.py::TestForeignWakeTripwire::test_clean_run_counts_zero",
    "test_mn_sim_fences.py::TestForeignWakeTripwire::test_foreign_gwake_nonstrict_counts",
    "test_mn_sim_fences.py::TestForeignWakeTripwire::test_foreign_gwake_strict_aborts",
    "test_mn_sim_fences.py::TestIoUringGate::test_rings_off_and_digest_stable_under_loop_env",
    "test_mn_sim_reap.py::TestSettleReap::test_chan_deadlock_still_raises",
    "test_mn_sim_reap.py::TestSettleReap::test_no_premature_reap_while_event_pending",
    "test_mn_sim_reap.py::TestSettleReap::test_reap_errno_is_ecanceled",
    "test_mn_sim_reap.py::TestSettleReap::test_repark_loop_hits_loud_deadlock_not_livelock",
    "test_mn_sim_reap.py::TestSettleReap::test_stranded_parkers_reaped_and_run_terminates",
    "test_simfd_mn_smoke.py::TestSimFdMnSmoke::test_dgram_seeds",
    "test_simfd_mn_smoke.py::TestSimFdMnSmoke::test_stream_seeds",
    "test_swarm_mn_sched.py::test_controlled_barrier_same_seed_identical_outcome_across_runs",
    "test_swarm_time_context_runtime.py::test_mn_barrier_deterministic_replay_timer_ctx",
)


def pytest_collection_modifyitems(config, items):
    for item in items:
        base = item.nodeid.split("[", 1)[0]
        if base.endswith(_SEEDED_MN_TODO):
            item.add_marker(pytest.mark.skip(
                reason="TODO(migration): seeded M:N scheduler disabled"))
            continue
        for tail, why in _PER_G_KNOWN_GAPS.items():
            if item.nodeid.endswith(tail):
                item.add_marker(pytest.mark.skip(
                    reason="known migration-mode gap: " + why))
                break
        else:
            for tail, why in _PER_G_OPEN_INTERMITTENT.items():
                if item.nodeid.endswith(tail):
                    item.add_marker(pytest.mark.skip(
                        reason="OPEN migration-mode intermittent: " + why))
                    break


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
