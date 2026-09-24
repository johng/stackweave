"""Discriminate WHY macOS drops a sim-delivered wake: capacity 0, or no init?

On macos-14 CI both interpreters fail
test_mn_sim_bytes::TestReviewRegressions::test_late_parker_gets_stashed_wake
with the receiver never seeing its byte, and this on stderr:

    [stackweave] fd 4 exceeds preallocated netpoll capacity 0; raise
              STACKWEAVE_NETPOLL_MAXFD (event dropped)
    OSError: [Errno 89] Operation canceled          <- ECANCELED (125 on Linux)

Capacity 0 cannot come from sizing.  runloom_fd_cap_target() starts at 65536,
prefers RLIMIT_NOFILE, and clamps to [1024, 8M] -- so even macOS's stingy
256-fd rlim_cur floors to 1024.  Capacity is 0 only if runloom_fd_arrays_init()
never ran, or its ~25 MB of calloc failed (implausible on a runner).  And that
function is called from exactly ONE place: runloom_netpoll_init().

So the hypothesis is an INIT-ORDERING bug on the kqueue path -- the sim wake is
stashed before netpoll has been brought up, the per-fd arrays do not exist, the
event is dropped, and the parker is cancelled.

The test runs the snippet in SIMULATION mode (STACKWEAVE_SIM=1, STACKWEAVE_SIM_MN=1
via mn_digest.hermetic_env), and that matters: the first version of this probe
omitted it, exercised the REAL netpoll path, and PASSED on macos-14 while the
test failed 5/5 on the same runner.  A probe that does not carry the failing
configuration only measures itself.

Four variants, run under the target interpreter:

    PYTHON_GIL=0 python tests/bughunt_repros/macos_netpoll_capacity_probe.py

  real        -- real netpoll. Control; expected to pass everywhere.
  sim         -- the configuration under test. Expected to reproduce
                 "capacity 0 -> event dropped -> ECANCELED" on macOS.
  sim+preinit -- rc.netpoll_poll() first, which routes through
                 runloom_netpoll_init() -> runloom_fd_arrays_init(). If `sim`
                 FAILS and this PASSES, the arrays simply were not allocated
                 yet: an ordering bug, fixed on the sim-delivery path.
  sim+maxfd   -- sim with STACKWEAVE_NETPOLL_MAXFD forced. Only informative if it
                 changes the outcome: that would mean capacity was computed and
                 merely too small (sizing), not skipped (ordering).

Exit 0 if every variant delivered the byte; 1 otherwise.  The point is the
per-variant table, not the exit code.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The CI snippet, verbatim from test_mn_sim_bytes, with an optional preamble.
BODY = """
import socket, stackweave_c as rc
%(preamble)s
a, b = socket.socketpair()
a.setblocking(False); b.setblocking(False)
cid = rc.sim_conn_register(a.fileno(), b.fileno())
got = {}
def sender():
    a.send(b'x')
    rc.sim_deliver_ready(cid, b.fileno(), 1)
def receiver():
    rc.sched_sleep(0.001)
    rc.wait_fd(b.fileno(), 1)
    got['d'] = b.recv(16)
rc.mn_init(2)
rc.mn_fiber(receiver)
rc.mn_fiber(sender)
rc.mn_run(); rc.mn_fini()
print('LATE_PARKER', got.get('d'))
print('BACKEND', rc.netpoll_backend())
"""

# The test runs this in SIMULATION mode -- test_mn_sim_bytes sets
#   SIM_ENV = {"STACKWEAVE_SIM": "1", "STACKWEAVE_SIM_MN": "1"}
# and passes it through mn_digest.hermetic_env (which strips every inherited
# RUNLOOM_* knob first, then pins PYTHON_GIL / PYTHONHASHSEED / PYTHONPATH).
# The FIRST version of this probe omitted that and therefore exercised the REAL
# netpoll path: it passed on macos-14 (run 33943464165) while the test failed
# 5/5 there, which told us only that the probe was wrong.  Sim mode is the
# configuration under test, so every meaningful variant carries it.
SIM = {"STACKWEAVE_SIM": "1", "STACKWEAVE_SIM_MN": "1", "STACKWEAVE_MN_SEED": "12345"}


def _sim(**extra):
    e = dict(SIM)
    e.update(extra)
    return e


VARIANTS = [
    # Control: the real netpoll path. Expected to pass everywhere; if it ever
    # fails, the problem is not sim-specific and this file is the wrong hunt.
    ("real", "", {}),
    # The configuration the test actually uses. This is the one expected to
    # reproduce "capacity 0 -> event dropped -> ECANCELED" on macOS.
    ("sim", "", _sim()),
    # netpoll_poll -> runloom_netpoll_pump -> runloom_netpoll_init ->
    # runloom_fd_arrays_init: brings the per-fd arrays up BEFORE any sim wake.
    ("sim+preinit", "rc.netpoll_poll()", _sim()),
    # Only informative if it changes the outcome: that would mean capacity was
    # computed and merely too small (sizing), not skipped (ordering).
    ("sim+maxfd", "", _sim(STACKWEAVE_NETPOLL_MAXFD="65536")),
]


def run(name, preamble, extra_env):
    # Mirror hermetic_env: strip inherited RUNLOOM_* so the parent's knobs
    # cannot contaminate the child, then pin exactly what the test pins.
    env = {k: v for k, v in os.environ.items() if not k.startswith("STACKWEAVE_")}
    env["PYTHON_GIL"] = "0"
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONPATH"] = os.path.join(REPO, "src")
    env.update(extra_env)
    p = subprocess.run([sys.executable, "-c", BODY % {"preamble": preamble}],
                       cwd=REPO, env=env, timeout=120,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.stdout or "", p.stderr or ""
    delivered = "LATE_PARKER b'x'" in out
    backend = next((l.split(" ", 1)[1] for l in out.splitlines()
                    if l.startswith("BACKEND ")), "?")
    cap0 = "preallocated netpoll capacity 0" in err
    cancel = "Operation canceled" in err or "Errno 89" in err or "Errno 125" in err
    print("  %-9s delivered=%-5s backend=%-8s capacity0=%-5s cancelled=%-5s rc=%d"
          % (name, delivered, backend, cap0, cancel, p.returncode))
    for line in err.strip().splitlines()[:4]:
        print("             | %s" % line)
    return delivered


def probe_closed_fd_liveness():
    """Which primitive actually reports a CLOSED fd on THIS platform?

    The kqueue dead-fd probe has to answer "is this descriptor still open"
    without touching kqueue state.  poll() with events==0 is the obvious
    choice and POSIX says POLLNVAL lands in revents regardless of events --
    but macOS poll() is quirky, and if it reports nothing for events==0 the
    probe silently never fires (which is exactly what a first attempt at the
    fix did: test_adv_netpoll stayed 5/5 with the parker still stranded).
    fcntl(F_GETFD) is the unambiguous alternative.  Measure, do not assume."""
    import errno as _errno
    import fcntl as _fcntl
    import select as _select
    r, w = os.pipe()
    os.close(r)
    os.close(w)
    # poll with events == 0
    try:
        po = _select.poll()
        po.register(r, 0)
        res = po.poll(0)
        nval = bool(res) and bool(res[0][1] & _select.POLLNVAL)
        print("  poll(events=0)   -> %r  POLLNVAL=%s" % (res, nval))
    except Exception as e:                       # noqa: BLE001
        print("  poll(events=0)   -> raised %r" % (e,))
    # poll with events == POLLIN, in case events==0 is the problem
    try:
        po = _select.poll()
        po.register(r, _select.POLLIN)
        res = po.poll(0)
        nval = bool(res) and bool(res[0][1] & _select.POLLNVAL)
        print("  poll(events=IN)  -> %r  POLLNVAL=%s" % (res, nval))
    except Exception as e:                       # noqa: BLE001
        print("  poll(events=IN)  -> raised %r" % (e,))
    # fcntl(F_GETFD)
    try:
        _fcntl.fcntl(r, _fcntl.F_GETFD)
        print("  fcntl(F_GETFD)   -> SUCCEEDED (reports the fd as alive!)")
    except OSError as e:
        print("  fcntl(F_GETFD)   -> OSError errno=%d (%s)%s"
              % (e.errno, _errno.errorcode.get(e.errno, "?"),
                 "  <- detects dead" if e.errno == _errno.EBADF else ""))


def probe_dead_fd_wake():
    """Reproduce test_adv_netpoll's scenario and say WHERE it stalls.

    Two fixes for that hang have now failed on macos-14 (the probe itself in
    00ccba5a, then the fcntl liveness switch in c156a4bd) -- both reasoned from
    source, neither measured.  This runs the scenario with
    STACKWEAVE_DBG_NETPOLL=1, which makes the kqueue validate_arm print
    "DEAD ARM cleared" if it ever reaches its dead branch.  That splits the
    remaining chain:

      no DEAD ARM line          -> the probe never fires: probe_pending is not
                                   set, or the sweep never collects the fd
                                   (look at wait_fd / drain_expired_pools)
      DEAD ARM line, still hung -> validate_arm works, the ERROR-WAKE does not
                                   (look at runloom_pump_dispatch_event)
      DEAD ARM line, returns    -> fixed

    The park MUST be untimed (-1), as the real test has it.  An earlier version
    of this probe capped it at 3000 ms so it would always return -- but the
    kqueue probe is scheduled only for untimed parks (timeout_ns < 0), so the
    cap silently disabled the mechanism under test and the run proved nothing
    (rv=0 at 3.002 s, no DEAD ARM line).  A probe that does not carry the
    failing configuration measures itself; this is the third time that has
    bitten in this investigation.
    """
    child = """
import os, sys, time
sys.path.insert(0, SRC)
import stackweave_c as rc
READ = 1
def f():
    r, w = os.pipe()
    rc.wait_fd(r, READ, 5)
    os.close(r); os.close(w)
    r2, w2 = os.pipe()
    if r2 != r:
        os.close(r2); os.close(w2)
        print("SKIP fd not reused"); return
    def closer():
        for _ in range(4):
            rc.sched_yield()
        os.close(r2); os.close(w2)
    rc.fiber(closer)
    t0 = time.monotonic()
    try:
        rv = rc.wait_fd(r2, READ, -1)            # UNTIMED, exactly as the test
        print("WAITFD rv=" + repr(rv) + " elapsed=%.3f" % (time.monotonic() - t0))
    except OSError as e:
        print("WAITFD raised " + repr(e) + " elapsed=%.3f" % (time.monotonic() - t0))
rc.fiber(f)
rc.run()
"""
    child = child.replace("SRC", repr(os.path.join(REPO, "src")))
    env = {k: v for k, v in os.environ.items() if not k.startswith("STACKWEAVE_")}
    env["PYTHON_GIL"] = "0"
    env["PYTHONPATH"] = os.path.join(REPO, "src")
    env["STACKWEAVE_DBG_NETPOLL"] = "1"
    env["STACKWEAVE_STALE_ARM_PROBE_MS"] = "100"   # well inside the 3 s cap
    try:
        p = subprocess.run([sys.executable, "-c", child], cwd=REPO, env=env,
                           timeout=20, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
    except subprocess.TimeoutExpired as e:
        # The child is killed here, so its output arrives on the EXCEPTION --
        # printing it is the whole point: the DEAD ARM line (or its absence)
        # is what distinguishes "probe never fired" from "probe fired but the
        # error-wake did not land".  An earlier version returned without
        # printing and threw that away.
        print("  TIMEOUT: the UNTIMED park was never woken.  (Failure reproduced.)")
        out = (e.output or b"")
        err = (e.stderr or b"")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        for line in out.strip().splitlines():
            print("  out| %s" % line)
        for line in err.strip().splitlines()[:8]:
            print("  err| %s" % line)
        if "DEAD ARM" in err:
            print("  => validate_arm DID reach its dead branch; the ERROR-WAKE "
                  "is what fails (runloom_pump_dispatch_event)")
        elif "STALE ARM" in err:
            print("  => only a HEAL fired, never a dead-clear")
        else:
            print("  => no probe output at all: probe_pending is not being set, "
                  "or the sweep never collects the fd")
        return
    for line in (p.stdout or "").strip().splitlines():
        print("  out| %s" % line)
    for line in (p.stderr or "").strip().splitlines()[:8]:
        print("  err| %s" % line)
    if "DEAD ARM" not in (p.stderr or ""):
        print("  => no DEAD ARM line: probe never reached validate_arm's dead branch")


def main():
    print("macos_netpoll_capacity_probe: %s" % sys.executable)
    print("\n-- closed-fd liveness primitives --")
    probe_closed_fd_liveness()
    print("\n-- dead-fd wake (test_adv_netpoll scenario) --")
    probe_dead_fd_wake()
    print("\n-- sim delivery variants --")
    ok = True
    for name, preamble, extra_env in VARIANTS:
        try:
            ok &= run(name, preamble, extra_env)
        except subprocess.TimeoutExpired:
            print("  %-9s TIMEOUT" % name)
            ok = False
    print()
    print("READING:")
    print("  real PASS + sim FAIL          -> the bug is in the SIM delivery")
    print("                                   path, not real netpoll.")
    print("  sim FAIL + sim+preinit PASS   -> init-ordering: the per-fd arrays")
    print("                                   were not allocated when the sim")
    print("                                   wake was stashed. Fix = init")
    print("                                   before stashing.")
    print("  sim FAIL + sim+preinit FAIL   -> not ordering; the kqueue sim")
    print("                                   delivery path itself.")
    print("  sim+maxfd changing anything   -> sizing, not ordering.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
