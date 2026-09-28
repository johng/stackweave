"""fiber_n spawn -> drain lifecycle at scale.

fiber_n(fn, N) spawns N fibers in one C loop, mn_run() drains all N on the hubs,
and the completed gs/coros/stacks return to their pools.  Repeated to stress
slab + stack-pool reuse across rounds.  The gate runs this under ASan/TSan, so a
lifecycle UAF or a lost fiber surfaces here.

Own file = own subprocess (run_isolated), so the low-level mn_init/mn_fini here
never shares runtime state with the high-level stackweave.run tests.
"""
import os

os.environ.setdefault("PYTHON_GIL", "0")
# Quiet sysmon (the spawn/drain lifecycle is scheduler-independent for this count check).
os.environ.setdefault("STACKWEAVE_SYSMON", "0")

import stackweave_c  # noqa: E402


def test_fiber_n_large_n_lifecycle_correct():
    N = 20000
    stackweave_c.mn_init(8)
    try:
        def worker():
            pass

        prev = 0
        for _ in range(3):                      # stress slab/stack-pool reuse
            stackweave_c.fiber_n(worker, N)
            done = stackweave_c.mn_run()           # cumulative completed count
            assert done - prev == N, (done - prev, N)
            prev = done
    finally:
        stackweave_c.mn_fini()


def test_fiber_n_small_n_still_correct():
    # Small batches must also drain fully.
    stackweave_c.mn_init(4)
    try:
        stackweave_c.fiber_n(lambda: None, 100)
        assert stackweave_c.mn_run() == 100
    finally:
        stackweave_c.mn_fini()
