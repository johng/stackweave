"""Bulk-spawn batch lifecycle at scale (docs/dev/spawn_experiments.md, Exp B).

Exercises the warm-stack arena + bulk + FRESH fast path: fiber_n(fn, N) builds
one big g/coro/stack batch, mn_run() drains all N, and the batch teardown returns
the slots.  Repeated to stress batch reset/reuse.  The gate runs this under
ASan/TSan, so a lifecycle UAF or a lost fiber surfaces here.

KNOWN GAP -- STACKWEAVE_GON_BULK IS BROKEN UNDER MIGRATION, SO IT IS IGNORED.
The bulk builder memcpy's a template g per arena slot from builder threads that
hold no PyThreadState, so no bulk fiber gets the per-g tstate every M:N fiber
needs, and hub_main skips a g with no tstate as dead: a bulk batch never runs.
runloom_fibern_bulk_enabled (mn_sched_init_fini.c.inc) therefore keeps fiber_n
on its per-fiber loop and says so once on stderr.  Every test here that needs
the bulk builder to RUN is a strict xfail (TODO_MIGRATION_FAIL), and asserts it
the only way the bulk path is observable: STACKWEAVE_GON_TIMING=1 makes the
builder print one "[GON_TIMING]" line per batch it built.  When the batch grows
a per-g tstate pass (and registers each g with runloom_greg_link, or the
parked-frame GC anchor cannot see bulk fibers -- see CLAUDE.md) and the guard
comes off, these turn into XPASS failures that force the markers off.

The stack arena the bulk path carves from (STACKWEAVE_STACK_ARENA) does work
under migration on its own; test_stack_arena_fiber_n_lifecycle covers it
through fiber_n's loop path.

Every case runs in its own subprocess: the gates are read once per process, and
the low-level mn_init/mn_fini here must never share runtime state with the
high-level stackweave.run tests.
"""
import os
import subprocess
import sys
import textwrap

import pytest

from adv_util import needs_free_threading

FT = needs_free_threading()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

pytestmark = pytest.mark.skipif(not FT, reason="M:N scheduler needs GIL-disabled build")

# The warm-stack arena + bulk + FRESH config the fast path was validated with.
BULK_ENV = {
    "STACKWEAVE_STACK_ARENA": "1",
    "STACKWEAVE_GON_BULK": "1",
    "STACKWEAVE_GON_FRESH": "1",
    "STACKWEAVE_GON_TIMING": "1",     # the bulk builder's only observable
}
BULK_IGNORED = "STACKWEAVE_GON_BULK ignored"


def TODO_MIGRATION_FAIL(reason):
    """Strict xfail for a known migration-mode gap (same convention as
    tests/test_cross_hub_migration.py): the moment the gap closes the test
    XPASSes and fails, forcing the marker off."""
    return pytest.mark.xfail(strict=True, reason="TODO_MIGRATION_FAIL: " + reason)


_BULK_GAP = TODO_MIGRATION_FAIL(
    "STACKWEAVE_GON_BULK is ignored under migration: the bulk fiber_n builder "
    "allocates no per-g tstate, so hub_main would skip every bulk fiber as dead")


def _run(body, env_extra, timeout=120):
    src = ("import sys\nsys.path.insert(0, 'src')\nimport stackweave_c\n"
           + textwrap.dedent(body))
    env = dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src",
               STACKWEAVE_SYSMON="0", **env_extra)
    for k in ("STACKWEAVE_GON_BULK", "STACKWEAVE_GON_FRESH", "STACKWEAVE_GON_TIMING",
              "STACKWEAVE_STACK_ARENA", "STACKWEAVE_STACK_ARENA_N"):
        if k not in env_extra:
            env.pop(k, None)
    return subprocess.run([PY, "-c", src], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=timeout)


def _assert_ran(p, marker):
    assert p.returncode == 0, (
        "worker crashed (rc=%d)\nstdout=%s\nstderr=%s"
        % (p.returncode, p.stdout[-1500:], p.stderr[-1500:]))
    assert marker in p.stdout, (
        "worker did not reach %r\nstdout=%s\nstderr=%s"
        % (marker, p.stdout[-1500:], p.stderr[-1500:]))


def _bulk_batches(p):
    return p.stderr.count("[GON_TIMING]")


_LARGE = """
    N = 20000
    stackweave_c.mn_init(8)
    try:
        def worker():
            pass
        prev = 0
        for _ in range(3):                      # stress batch reset/reuse
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


@_BULK_GAP
def test_gon_bulk_takes_bulk_path_under_migration():
    """The gap, stated once: with GON_BULK=1, fiber_n(N) must run every fiber
    AND build them with the bulk builder.  Today every fiber runs (the guard
    routes fiber_n to its per-fiber loop) but no bulk batch is ever built."""
    p = _run("""
        N = 2000
        stackweave_c.mn_init(4)
        try:
            stackweave_c.fiber_n(lambda: None, N)
            assert stackweave_c.mn_run() == N
        finally:
            stackweave_c.mn_fini()
        print("BULK_RAN_OK")
    """, BULK_ENV)
    _assert_ran(p, "BULK_RAN_OK")
    assert BULK_IGNORED not in p.stderr, p.stderr[-800:]
    assert _bulk_batches(p) == 1, (
        "fiber_n did not take the bulk path:\n%s" % p.stderr[-800:])


def test_gon_bulk_is_ignored_safely_under_migration():
    """The guard's promise: GON_BULK=1 costs nothing but the fast path -- every
    fiber still runs, and the reason is on stderr exactly once."""
    p = _run("""
        stackweave_c.mn_init(4)
        try:
            for _ in range(3):
                stackweave_c.fiber_n(lambda: None, 500)
            assert stackweave_c.mn_run() == 1500
        finally:
            stackweave_c.mn_fini()
        print("IGNORED_OK")
    """, BULK_ENV)
    _assert_ran(p, "IGNORED_OK")
    assert p.stderr.count(BULK_IGNORED) == 1, p.stderr[-800:]
    assert _bulk_batches(p) == 0, p.stderr[-800:]


@_BULK_GAP
def test_bulk_large_n_lifecycle_correct():
    p = _run(_LARGE, BULK_ENV)
    _assert_ran(p, "LARGE_OK")
    assert _bulk_batches(p) == 3, p.stderr[-800:]


@_BULK_GAP
def test_bulk_small_n_still_correct():
    p = _run(_SMALL, BULK_ENV)
    _assert_ran(p, "SMALL_OK")
    assert _bulk_batches(p) == 1, p.stderr[-800:]


def test_stack_arena_fiber_n_lifecycle():
    """STACKWEAVE_STACK_ARENA=1 alone: every fiber_n fiber carves its stack from
    the per-size arena and returns the slot on completion, and the cursor
    resets between rounds.  Works under migration (the arena is only a stack
    allocator); runs through fiber_n's per-fiber loop."""
    p = _run(_LARGE, {"STACKWEAVE_STACK_ARENA": "1"})
    _assert_ran(p, "LARGE_OK")
    p = _run(_SMALL, {"STACKWEAVE_STACK_ARENA": "1"})
    _assert_ran(p, "SMALL_OK")
