"""Fibers run on their hub's thread-local bytecode copy, and stay correct.

Free-threaded CPython gives each thread state its own copy of every code
object's bytecode (TLBC), because the specializer rewrites instructions and
inline caches without atomics.  On an interpreter with the exec-home patch's
TLBC hunk, built with -DPy_TSTATE_TLBC_FOLLOW (it exports _PyEval_TLBCFollow:
stackweave_c.tlbc_follow), each resume lends the fiber its hub's index
(runloom_iframe_tlbc_lend), so live fibers share one copy per hub
instead of owning one each (stackweave_c.tlbc_shared == 1), and the eval loop
re-points a frame at the new hub's copy before the next instruction after a
migration.  Without that hunk, lending would let two hubs run and specialize
one copy at once: tools/probes/ft_locks/p23_tlbc_shared_stress.py segfaults
in seconds when lending is forced on without it.

These run p23 -- polymorphic sites that keep re-specializing, every fiber
parking mid-function and resuming on other hubs -- and check every result.
On an interpreter without the hunk (tlbc_shared == 0) fibers keep their own
copies and the same checks must still pass; the cross-hub switch count is
then 0 by construction.  The memory side is
tests/test_cpython_thread_keyed_locks.py::
test_cost_live_fibers_share_bytecode_within_1_5x_of_tlbc_off.
"""
import os
import re
import sys

import stackweave_c
from adv_util import REPO, run_python

PROBES = os.path.join(REPO, "tools", "probes", "ft_locks")
STRESS = os.path.join(PROBES, "p23_tlbc_shared_stress.py")


def _stress(*args, timeout=240):
    code = ("import runpy, sys\n"
            "sys.path.insert(0, %r)\n"
            "sys.argv = [%r] + %r\n"
            "runpy.run_path(%r, run_name='__main__')\n"
            % (PROBES, STRESS, [str(a) for a in args], STRESS))
    p = run_python(code, timeout=timeout, cwd=PROBES)
    out = p.stdout + p.stderr
    assert p.returncode == 0 and "PASS" in p.stdout, out[-4000:]
    m = re.search(r"tlbc_shared=(\d) tlbc_switches=(\d+)", p.stdout)
    assert m, out[-2000:]
    return int(m.group(1)), int(m.group(2))


def _check_ran(shared, switches):
    assert shared == stackweave_c.tlbc_shared
    if shared:
        # The fibers really did resume on other hubs' copies.
        assert switches > 0, "no fiber resumed on another hub's copy"
    else:
        assert switches == 0


def test_fibers_share_hub_copies_exactly_when_the_interpreter_follows():
    """tlbc_follow: the interpreter exports _PyEval_TLBCFollow (built with
    -DPy_TSTATE_TLBC_FOLLOW).  Fibers share hub copies iff it does and TLBC
    is on."""
    tlbc_on = sys._xoptions.get("tlbc", os.environ.get("PYTHON_TLBC", "1")) != "0"
    assert stackweave_c.tlbc_shared == (stackweave_c.tlbc_follow and tlbc_on)


def test_specialized_code_stays_right_when_fibers_change_hub_copies():
    _check_ran(*_stress(8, 64, 400))


def test_clear_internal_caches_mid_run_keeps_the_copies_parked_frames_use():
    """sys._clear_internal_caches() frees every copy no live thread state
    holds.  A parked fiber's frames point into its hub's copy while the hub's
    own state (or the fiber running there) holds that index, so the copies
    they resume on are never freed under them."""
    _check_ran(*_stress(4, 32, 300, "clear"))


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
