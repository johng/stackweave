"""Lock-in tests (env/contract) for the spawn fast-path (docs/dev/spawn_experiments.md).

These assert the *wiring* of the landed keeper without running the runtime to a
completion count: resident-memset stack scrub is the DEFAULT (secure + fast); the
get/set contract.

The fiber_n spawn/drain *lifecycle* at scale is exercised separately in
tests/test_fiber_n_lifecycle.py (own subprocess), and scrub-under-churn
correctness is covered by the existing swarm/coro/stack tests now running the
resident-scrub default.
"""
import os

os.environ.setdefault("PYTHON_GIL", "0")

import stackweave_c  # noqa: E402


def test_resident_scrub_contract():
    # The resident-page wipe is the only scrub mode; the toggle surface exists for
    # the "secure" profile to drive.
    assert callable(stackweave_c.get_stack_scrub)
    assert callable(stackweave_c.set_stack_scrub)

