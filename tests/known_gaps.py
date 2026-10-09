"""Markers for the suite's known gaps.

A known gap is a test that asserts the CORRECT behaviour and fails today.  It
is an xfail, never a skip, so it still runs on every pass.  It is strict: when
the gap closes, the test XPASSes, which fails the run until the marker comes
off.  Then keep a positive check that the fixed path ran (CLAUDE.md, "Broken
features are kept, not deleted").

Mark a known gap with one of these and never with a bare pytest.mark.xfail.
conftest.py rejects any xfail whose reason lacks one of the PREFIXES below, and
tags every known gap with the `known_gap` marker, so `pytest -m known_gap`
lists them all.

  KNOWN_GAP(reason)       an open runtime bug.
  MIGRATION_GAP(reason)   breaks because a fiber can change OS thread, or
                          because each fiber has its own thread state.  Both
                          come with cross-hub migration, which is always on.
  SEEDED_MN_TODO          drives the seeded M:N scheduler, which is disabled
                          until it is re-implemented: mn_init refuses a seeded
                          run.
  GON_BULK_GAP            needs STACKWEAVE_GON_BULK's bulk spawn, which is
                          ignored under migration.
  INTERMITTENT(reason)    an open failure that does not happen on every run.
                          It is not strict, because an XPASS does not mean the
                          gap has closed.

raises= narrows what counts as the known break.  A test that first checks that
its scenario reached the trigger, and calls pytest.fail when it did not, passes
raises=AssertionError.  A missed trigger then stays a real failure.
"""
import pytest

PREFIXES = ("KNOWN GAP: ", "MIGRATION GAP: ", "SEEDED M:N TODO: ",
            "INTERMITTENT: ")


def _xfail(prefix, reason, strict, raises):
    return pytest.mark.xfail(strict=strict, raises=raises,
                             reason=prefix + reason)


def KNOWN_GAP(reason, *, raises=None, strict=True):
    return _xfail("KNOWN GAP: ", reason, strict, raises)


def MIGRATION_GAP(reason, *, raises=None, strict=True):
    return _xfail("MIGRATION GAP: ", reason, strict, raises)


def INTERMITTENT(reason, *, raises=None):
    return _xfail("INTERMITTENT: ", reason, False, raises)


SEEDED_MN_TODO = _xfail(
    "SEEDED M:N TODO: ",
    "the seeded M:N scheduler is disabled under migration and mn_init "
    "refuses a seeded run: woken fibers run from the global run-queue, which "
    "the seeded baton does not order (TODO(migration) in "
    "src/runloom_c/mn_sched_hub_resume_preempt.c.inc)",
    True, None)

GON_BULK_GAP = MIGRATION_GAP(
    "STACKWEAVE_GON_BULK is ignored: the bulk fiber_n builder allocates no "
    "per-g thread state, so hub_main would skip every bulk fiber as dead "
    "(tests/test_spawn_bulk_lifecycle.py)")
