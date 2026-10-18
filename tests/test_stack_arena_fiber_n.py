"""fiber_n under the stack arena (STACKWEAVE_STACK_ARENA=1), at scale.

Every fiber_n fiber carves its stack from the per-size arena and returns the
slot on completion, and the arena's cursor resets between rounds; repeated
rounds stress that reset/reuse.  The gate runs this under ASan/TSan, so a
lifecycle UAF or a lost fiber surfaces here.

Every case runs in its own subprocess: the gates are read once per process, and
the low-level mn_init/mn_fini here must never share runtime state with the
high-level stackweave.run tests.
"""
import sys
import textwrap

import pytest

from adv_util import run_python


def _run(body, env_extra, timeout=120):
    env = dict.fromkeys(("STACKWEAVE_STACK_ARENA",
                         "STACKWEAVE_STACK_ARENA_N"))   # None: unset unless asked
    env.update(STACKWEAVE_SYSMON="0", **env_extra)
    return run_python("import stackweave_c\n" + textwrap.dedent(body),
                      env=env, timeout=timeout)


def _assert_ran(p, marker):
    assert p.returncode == 0, (
        "worker crashed (rc=%d)\nstdout=%s\nstderr=%s"
        % (p.returncode, p.stdout[-1500:], p.stderr[-1500:]))
    assert marker in p.stdout, (
        "worker did not reach %r\nstdout=%s\nstderr=%s"
        % (marker, p.stdout[-1500:], p.stderr[-1500:]))


_LARGE = """
    N = 20000
    stackweave_c.mn_init(8)
    try:
        def worker():
            pass
        prev = 0
        for _ in range(3):                      # stress arena reset/reuse
            stackweave_c.fiber_n(worker, N)
            done = stackweave_c.mn_run()           # cumulative completed count
            assert done - prev == N, (done - prev, N)
            prev = done
    finally:
        stackweave_c.mn_fini()
    print("LARGE_OK")
"""

_SMALL = """
    # Small batches must also drain fully (the arena lazy-inits its size class here).
    stackweave_c.mn_init(4)
    try:
        stackweave_c.fiber_n(lambda: None, 100)
        assert stackweave_c.mn_run() == 100
    finally:
        stackweave_c.mn_fini()
    print("SMALL_OK")
"""


def test_stack_arena_fiber_n_lifecycle():
    """STACKWEAVE_STACK_ARENA=1 alone: every fiber_n fiber carves its stack from
    the per-size arena and returns the slot on completion, and the cursor
    resets between rounds.  Works under migration (the arena is only a stack
    allocator); runs through fiber_n's per-fiber loop."""
    p = _run(_LARGE, {"STACKWEAVE_STACK_ARENA": "1"})
    _assert_ran(p, "LARGE_OK")
    p = _run(_SMALL, {"STACKWEAVE_STACK_ARENA": "1"})
    _assert_ran(p, "SMALL_OK")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
