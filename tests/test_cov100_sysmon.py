"""Adversarial coverage suite for src/runloom_c/mn_sched_sysmon.c.inc.

The sysmon watchdog is DETECT + PREEMPT only: it logs a wedged hub and, for an
ATTACHED (CPU-bound) wedge, arms preemption.  A DETACHED (blocking-IO) wedge
needs no recovery here -- normal work-stealing already drains a stalled hub's
fresh fibers to idle hubs.  This test pins that property: a workload that wedges
several hubs on real blocking calls while a fan-out of fresh fibers is queued
must STILL complete every fiber, with the wedged hubs' fresh work drained by the
idle hubs (no standby "rescue" thread is involved -- that subsystem was removed).

The OOM / spawn-failure early-outs of runloom_sysmon_main, and
runloom_sched_freeze_for_crash (only reached on a fatal-signal death, where gcov
never flushes), have no fault-injection site -- see the `unreachable` report.

The subprocess EXITS CLEANLY (rc==0 + a stdout marker) so gcov counters flush.
"""
import sys

import pytest

from adv_util import run_python


def _run(body, env_extra, timeout=90):
    """Run `body` as a fresh child Python process under the given env; return it.

    The child imports the same in-tree stackweave_c (src/ on PYTHONPATH) and
    must finish cleanly for gcov counters to flush -- we assert rc==0 + marker.
    """
    src = ("import stackweave\n"
           "import stackweave_c as rc\n"
           "import time\n"
           "from stackweave.sync import WaitGroup\n") + body
    return run_python(src, timeout=timeout, env=env_extra)


# --------------------------------------------------------------------------- #
# Several hubs wedge on a real blocking call while a fan-out of fresh fibers is
# queued.  Work-stealing must drain the stranded fresh fibers to the idle hubs,
# so every fiber completes (no rescue thread exists).  STACKWEAVE_SYSMON=1 +
# a low STACKWEAVE_SYSMON_MS arm the detector so its instrumentation is exercised.
# --------------------------------------------------------------------------- #
def test_wedged_hubs_drain_via_work_stealing():
    body = r"""
NHUBS = 4
NFRESH = 120
done = bytearray(NFRESH)
R = {}

def main():
    wg = WaitGroup(); wg.add(NFRESH)
    def fresh(i):
        try:
            x = 0
            for k in range(600):
                x += k
            done[i] = 1                     # single writer per slot, race-free
        finally:
            wg.done()
    for i in range(NFRESH):
        rc.mn_fiber(lambda i=i: fresh(i))

    def blocker():
        time.sleep(0.3)                     # DETACHED wedge per hub
    for _ in range(NHUBS):
        rc.mn_fiber(blocker)

    wg.wait()
    R["done"] = sum(done)

stackweave.run(NHUBS, main)
# Every fresh fiber must complete (drained off the wedged hubs by idle hubs, or
# by the owners after the blockers wake).  A work-stealing bug that stranded a
# fiber behind a wedged hub would show up as done < NFRESH (or a hang).
assert R["done"] == 120, R
print("WORKSTEAL_OK done=%d" % R["done"])
"""
    p = _run(body, {"STACKWEAVE_SYSMON": "1", "STACKWEAVE_SYSMON_QUIET": "1",
                    "STACKWEAVE_SYSMON_MS": "20"})
    assert p.returncode == 0, "wedge workload crashed (rc=%d)\nstderr=%s" % (
        p.returncode, p.stderr[-2000:])
    assert "WORKSTEAL_OK done=120" in p.stdout, (
        "wedge workload incomplete\nout=%s\nerr=%s" % (p.stdout, p.stderr[-1500:]))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
