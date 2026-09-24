"""sysmon-as-oracle: use the runtime's own stall detector to prove the
cooperative property end-to-end.

The M:N scheduler ships a sysmon watchdog (default-on on free-threaded 3.13t)
that logs `[STACKWEAVE_SYSMON] hub N WEDGED ...` when a fiber pins a hub past the
budget without yielding.  We use that as a test oracle:

  * a workload that does NOT cooperate (unwrapped CPU-heavy hashing inline)
    WEDGES -- which also proves the detector itself works, so a "no WEDGE" is
    meaningful;
  * the SAME workload with `heavy` auto-offload on produces no DETACHED wedge --
    the hashes relocate to the pool and the fibers park, so no hub is pinned in
    a GIL-released hash.  (Not "no wedge at all": the budget is wall clock, so a
    hub merely descheduled by a busy OS also trips it.  See that test.)
  * a purely cooperative workload (cooperative sleeps) never wedges (no false
    positives).

Each case runs in its own subprocess (needs mn_init + a low STACKWEAVE_SYSMON_MS, and
the WEDGED line is a C fprintf to stderr).
"""
import os
import re
import subprocess
import sys
import textwrap
import unittest

def _run(snippet, sysmon_ms=20, timeout=90):
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    env["STACKWEAVE_SYSMON_MS"] = str(sysmon_ms)
    # WEDGED/RECOVERED stderr lines are the oracle here, and since b9221d8e
    # sysmon logs them only on explicit opt-in (quiet when it runs solely to
    # service preemption) -- so opt in.
    env["STACKWEAVE_SYSMON"] = "1"
    env.setdefault("PYTHON_GIL", "0")
    p = subprocess.run([sys.executable, "-c", snippet],
                       capture_output=True, text=True, timeout=timeout, env=env)
    out = p.stdout + p.stderr
    # Every oracle here reads the ABSENCE or PRESENCE of a log line, so a
    # subprocess that died before it got going would satisfy the "absence"
    # tests vacuously -- a mutation run proved exactly that, passing in 0.115s
    # on a snippet with a syntax error.  The workload must actually have run.
    assert p.returncode == 0, (
        "sysmon workload exited %r -- the oracle below would be vacuous:\n%s"
        % (p.returncode, out))
    return out


_HASH_WORKLOAD = textwrap.dedent("""
    import sys, hashlib, stackweave, stackweave.monkey, stackweave_c
    stackweave.monkey.patch(heavy=({heavy}))
    BUF = b"x" * (8 * 1024 * 1024)
    def g():
        for _ in range(25):
            hashlib.sha256(BUF).digest()
    stackweave_c.mn_init(4)
    for _ in range(4):
        stackweave_c.mn_fiber(g)
    stackweave_c.mn_run()
""")


_WEDGE_RE = re.compile(
    r"hub (\d+) WEDGED ([\d.]+) ms .*?tstate=([A-Z]+)")


def _wedges(out):
    """[(hub, ms, tstate)] parsed from the sysmon log."""
    return [(int(h), float(ms), st) for h, ms, st in _WEDGE_RE.findall(out)]


class TestSysmonOracle(unittest.TestCase):
    def test_unwrapped_heavy_wedges(self):
        """Negative control: inline CPU-heavy hashing pins hubs -> WEDGED.
        Proves the detector fires (so the positive test's silence is real).

        This also PINS THE CLASSIFICATION the positive test keys on: hashlib
        releases the GIL, so an un-offloaded hash wedges DETACHED.  If that ever
        changes, this control fails loudly rather than letting
        test_heavy_autooffload_prevents_wedge quietly lose all its power."""
        out = _run(_HASH_WORKLOAD.format(heavy="False"))
        self.assertIn("WEDGED", out,
                      "expected the sysmon detector to flag the inline stall")
        states = {st for _, _, st in _wedges(out)}
        self.assertIn("DETACHED", states,
                      "inline hashing must wedge DETACHED -- the positive test "
                      "keys on that signature:\n" + out)

    def test_attached_cpu_loop_classified(self):
        """A frameless pure-Python loop is the true ATTACHED class -- it holds
        the tstate (no GIL release) and makes no Python calls, so preemption's
        eval-frame hook can't break it.  The detector must flag it AND classify
        it ATTACHED; this is the case offload() exists for."""
        snippet = textwrap.dedent("""
            import stackweave_c
            def hog():
                i = 0
                while i < 80_000_000:
                    i += 1
            stackweave_c.mn_init(4)
            for _ in range(4):
                stackweave_c.mn_fiber(hog)
            stackweave_c.mn_run()
        """)
        out = _run(snippet)
        self.assertIn("WEDGED", out)
        self.assertIn("ATTACHED", out)

    def test_heavy_autooffload_prevents_wedge(self):
        """The money test: with `heavy` on (default), the same hashing
        auto-offloads to the pool, the fibers park, and no hub is PINNED BY THE
        HASH.

        Asserted as "no DETACHED wedge", not "no WEDGED line at all".  sysmon's
        budget is 20ms of WALL CLOCK (STACKWEAVE_SYSMON_MS above), and wall clock
        cannot tell "a fiber is pinning this hub" apart from "the OS did not
        schedule this hub thread for 20ms".  This workload asks for 4 hubs plus
        blockpool workers each hashing 8 MiB, which oversubscribes a 3-core CI
        runner, so the second reading happens routinely: on macOS it flaked ~1
        run in 5 with `tstate=ATTACHED ... 0 g(s) stranded`, and it reproduces
        on Linux by running this exact snippet under 3x CPU oversubscription
        (measured: ATTACHED and SUSPENDED wedges, while heavy was offloading
        correctly the whole time).

        DETACHED is the signature of the regression this test exists to catch --
        an un-offloaded hash holds no tstate because hashlib drops the GIL, and
        test_unwrapped_heavy_wedges above proves inline hashing produces exactly
        that (measured 4/4 DETACHED).  So this keeps full power over "heavy
        stopped offloading" while dropping the reading that only measures how
        busy the machine is.  ATTACHED pinning is covered, unambiguously and
        without a load-sensitive oracle, by test_attached_cpu_loop_classified.
        """
        out = _run(_HASH_WORKLOAD.format(heavy="True"))
        detached = [w for w in _wedges(out) if w[2] == "DETACHED"]
        self.assertEqual(detached, [],
                         "auto-offloaded hashing must never pin a hub in a "
                         "GIL-released hash (heavy stopped offloading?):\n" + out)

    def test_cooperative_workload_never_wedges(self):
        """No false positives: cooperative sleeps park every few ms."""
        snippet = textwrap.dedent("""
            import stackweave, stackweave.monkey, stackweave_c
            stackweave.monkey.patch()
            def g():
                for _ in range(60):
                    stackweave.sleep(0.005)
            stackweave_c.mn_init(4)
            for _ in range(8):
                stackweave_c.mn_fiber(g)
            stackweave_c.mn_run()
        """)
        out = _run(snippet)
        self.assertNotIn("WEDGED", out, out)


if __name__ == "__main__":
    unittest.main()
