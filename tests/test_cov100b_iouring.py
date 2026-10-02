"""Round-2 coverage recovery: the io_uring LOOP backend paths.

The round-1 cov100 suites drove real workloads but under the DEFAULT (epoll
readiness) backend, so the io_uring-as-loop code -- the per-hub ring arm/teardown
in hub_main, and the CQE-driven wake/cancel machinery in netpoll_wake_iouring --
never executed.  These tests run the SAME adversarial workloads with
STACKWEAVE_IOURING_LOOP=1 (+ STACKWEAVE_IOURING_MS=1 for multishot recv) in a
SUBPROCESS (the backend is resolved once at first run(), so it must be set in the
child env), and each child EXITS CLEANLY so gcov counters flush.

Oracles are real: exact-once byte echo, a closed-form channel sum, a clean
teardown across many ring create/destroy cycles, and cancel-wakes a fiber parked
on an in-flight io_uring op (asserts it returns CANCELLED, not hangs).
"""
import errno
import os
import platform
import re
import subprocess
import sys

import pytest

from adv_util import (IOURING_LOOP_TRAILER, assert_iouring_loop_ran,
                      kernel_needs_pbuf_resv_quirk, kernel_pbuf_ring_errno,
                      needs_free_threading)

FT = needs_free_threading()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

pytestmark = pytest.mark.skipif(not FT, reason="io_uring loop is an M:N backend")


def _iou_available():
    try:
        import stackweave_c
        return bool(stackweave_c.iouring_available())
    except Exception:
        return False


needs_iouring = pytest.mark.skipif(not _iou_available(), reason="io_uring unavailable")


def _run(script, env_extra, timeout=240):
    # Generous timeout: these io_uring-loop workloads can run slow under a loaded
    # box (a concurrent build/CI run competing for io_uring + CPU); a timeout
    # there is contention, not a bug.  We make them robust rather than flaky.
    env = dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src",
               STACKWEAVE_IOURING_LOOP="1", STACKWEAVE_IOURING_MS="1", **env_extra)
    try:
        return subprocess.run([PY, "-c", script], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.skip("io_uring-loop workload timed out (box under heavy load)")


# --------------------------------------------------------------------------
# 1. concurrent TCP echo under the io_uring loop: drives ring recv/send + the
#    cross-hub CQE wake path.  Exact-once byte oracle.
# --------------------------------------------------------------------------
_ECHO = r'''
import sys, struct; sys.path.insert(0, "src")
import stackweave, stackweave_c as rc
from stackweave.sync import WaitGroup
N = 64
got = [None] * N
def main():
    port, lst = rc.serve("127.0.0.1", 0, None, 3)   # all-C echo on the io_uring loop
    wg = WaitGroup(); wg.add(N)
    def client(i):
        try:
            c = rc.TCPConn.connect("127.0.0.1", port)
            c.send_all(struct.pack(">Q", i))
            got[i] = c.recv(8)
            c.close()
        finally:
            wg.done()
    for i in range(N):
        rc.mn_fiber(lambda i=i: client(i))
    wg.wait()
    for ln in lst:
        ln.close()
stackweave.run(4, main)
ok = sum(1 for i in range(N) if got[i] == struct.pack(">Q", i))
sys.stdout.write("ECHO_OK %d\n" % ok)
'''


@needs_iouring
def test_iouring_loop_echo_exact_once():
    p = _run(_ECHO + IOURING_LOOP_TRAILER, {})
    assert p.returncode == 0, (p.stdout[-400:], p.stderr[-1200:])
    assert "ECHO_OK 64" in p.stdout, (p.stdout[-400:], p.stderr[-800:])
    assert_iouring_loop_ran(p)


# --------------------------------------------------------------------------
# 1b. the all-C echo keeps working while its fibers migrate between hubs.
#     Every park is a possible migration, and each hub ring has ONE lock-free
#     SQ producer (its hub).  The echo used to cache its hub's ring for the
#     whole connection, so after a migration it wrote SQEs into another hub's
#     ring from a foreign thread and a lost SQE parked the fiber forever.  Many
#     round trips per connection on 4 hubs give every echo fiber hundreds of
#     parks.  A hang must FAIL, not skip like _run's timeout, hence the
#     in-child watchdog.  multishot=0 covers the per-op ring lookup of
#     loop_recv/loop_send; multishot=1 the stream's owner-hub inbox, and it
#     asserts buffers really were returned from another hub (the fibers
#     migrated while their stream was open), or the test would prove nothing.
#
#     multishot=1 rests on two things the oracles above can't see, because a
#     run without either moves the same bytes:
#       - multishot armed at all.  A stream falls back to single-shot when its
#         hub has no provided buffer ring, and the kernel can refuse one
#         (Ubuntu's 6.8.0-142-generic refuses every valid registration).  All
#         48 streams must open; the case skips only when an independent probe
#         shows the KERNEL refuses the registration, and fails when the
#         runtime's own registration fails on a kernel that accepts it.
#       - a fiber migrated with its stream open.  A woken echo fiber lands on
#         its waker's deque, i.e. its stream's owner hub (local wake), and
#         moves only if an idle hub steals it first, so some runs have no
#         migration at all (about 1 in 200 on an idle 16-CPU box).
#         POSTED_RETURNS counts the buffers fibers finished off their stream's
#         hub; zero means no migration, not a bug, so that run is repeated,
#         and the case skips only if every attempt had none.  Every posted
#         buffer must get back to its owner (REMOTE_RETURNS).
# --------------------------------------------------------------------------
_ECHO_MIGRATE = r"""
import sys, struct, faulthandler; sys.path.insert(0, "src")
faulthandler.dump_traceback_later(60, exit=True)
import stackweave, stackweave_c as rc
from stackweave.sync import WaitGroup
N, ROUNDS = 48, 200
ok = bytearray(N)
def recv_exact(c, n):
    buf = b""
    while len(buf) < n:
        d = c.recv(n - len(buf))
        if not d:
            break
        buf += d
    return buf
def main():
    port, lst = rc.serve("127.0.0.1", 0, None, 4)   # all-C echo, one acceptor per hub
    wg = WaitGroup(); wg.add(N)
    def client(i):
        try:
            c = rc.TCPConn.connect("127.0.0.1", port)
            good = True
            for r in range(ROUNDS):
                msg = struct.pack(">II", i, r)
                c.send_all(msg)
                if recv_exact(c, 8) != msg:
                    good = False
            c.close()
            ok[i] = good
        finally:
            wg.done()
    for i in range(N):
        rc.mn_fiber(lambda i=i: client(i))
    wg.wait()
    for ln in lst:
        ln.close()
stackweave.run(4, main)
faulthandler.cancel_dump_traceback_later()
st = rc.stats()
sys.stdout.write("MIGRATE_OK %d\n" % sum(ok))
sys.stdout.write("LOOP_POLLS %d\n" % st["iouring_loop_polls"])
sys.stdout.write("MS_OPENS %d\n" % st["iouring_loop_ms_opens"])
sys.stdout.write("MS_FALLBACKS %d\n" % st["iouring_loop_ms_fallbacks"])
sys.stdout.write("MS_PBUF_ERRNO %d\n" % st["iouring_loop_ms_pbuf_errno"])
sys.stdout.write("PBUF_RESV_QUIRK %d\n" % st["iouring_pbuf_resv_quirk"])
sys.stdout.write("POSTED_RETURNS %d\n" % st["iouring_loop_ms_posted_returns"])
sys.stdout.write("REMOTE_RETURNS %d\n" % st["iouring_loop_ms_remote_returns"])
"""

# A run with no fiber finishing a buffer off its stream's hub is rare (1 in
# ~800 measured on a 16-core box), so four in a row means migration with a
# stream open has stopped happening -- a regression this guard exists to catch.
_MIGRATE_ATTEMPTS = 4


def _run_echo_migrate(multishot, **env_extra):
    """One echo run, checked against the oracles that hold with or without
    multishot; returns (CompletedProcess, {stat: int})."""
    env = dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src",
               STACKWEAVE_IOURING_LOOP="1", STACKWEAVE_IOURING_MS=multishot)
    env.pop("STACKWEAVE_IOURING_PBUF_RESV_QUIRK", None)
    env.update(env_extra)
    p = subprocess.run([PY, "-c", _ECHO_MIGRATE + IOURING_LOOP_TRAILER],
                       cwd=REPO, env=env, capture_output=True, text=True,
                       timeout=240)
    assert p.returncode == 0, (
        "echo run failed or hung (the watchdog exits 1 after 60 s)\n"
        "stdout=%s\nstderr=%s" % (p.stdout[-400:], p.stderr[-2000:]))
    assert "MIGRATE_OK 48" in p.stdout, (p.stdout[-400:], p.stderr[-800:])
    assert_iouring_loop_ran(p)
    st = {k: int(v) for k, v in re.findall(r"^([A-Z_]+) (-?\d+)$",
                                            p.stdout, re.M)}
    # Completions must be served BETWEEN fibers (the per-round loop_poll at
    # the pick step or the 64-turn self-pump), not only when a hub idles: with
    # 4 hubs and 96 fibers the hubs rarely idle, so an idle-only service would
    # leave ops waiting a whole busy stretch.
    assert st["LOOP_POLLS"] > 0, (
        "no per-round ring poll drained a completion\n" + p.stdout[-400:])
    return p, st


@needs_iouring
@pytest.mark.parametrize("multishot", ["1", "0"])
def test_iouring_loop_echo_survives_fiber_migration(multishot):
    p, st = _run_echo_migrate(multishot)
    if multishot == "0":
        return
    quirk = kernel_needs_pbuf_resv_quirk()
    if st["MS_OPENS"] == 0:
        kerr = kernel_pbuf_ring_errno()
        assert kerr and not quirk, (
            "multishot never armed, yet %s: the runtime's own buffer-ring "
            "registration failed\n%s\n%s"
            % ("this kernel accepts a provided buffer ring" if kerr == 0
               else "the kernel could not be probed" if kerr is None
               else "this kernel accepts the Ubuntu 6.8 workaround's form",
               p.stdout[-400:], p.stderr[-800:]))
        # The fallback must be visible: the errno stat and the one-time
        # warning are what tell this run from a multishot one.
        assert st["MS_PBUF_ERRNO"] == kerr, (
            "the hubs' buffer-ring failure was not recorded as the kernel's "
            "errno %d\n%s" % (kerr, p.stdout[-400:]))
        assert "provided buffer ring could not be registered" in p.stderr, (
            "no capability-degrade warning\n" + p.stderr[-800:])
        pytest.skip(
            "multishot recv never armed: this kernel (%s) refuses every "
            "io_uring provided buffer ring (errno %d, %s), so all 48 streams "
            "fell back to single-shot recv, which passed the run's oracles "
            "and is what the [0] case covers"
            % (platform.release(), kerr, os.strerror(kerr)))
    # The Ubuntu 6.8 workaround engages exactly where the kernel needs it, and
    # says so.
    assert st["PBUF_RESV_QUIRK"] == int(quirk), (
        "the workaround %s\n%s"
        % ("never engaged on a kernel that needs it" if quirk
           else "engaged on a kernel that takes the plain registration",
           p.stdout[-400:]))
    assert ("inverted check" in p.stderr) == quirk, (
        "the workaround's one-time notice is %s\n%s"
        % ("missing" if quirk else "printed without it", p.stderr[-800:]))
    for attempt in range(1, _MIGRATE_ATTEMPTS + 1):
        if attempt > 1:
            p, st = _run_echo_migrate(multishot)
        assert st["MS_OPENS"] == 48 and st["MS_FALLBACKS"] == 0, (
            "not every echo stream ran multishot\n" + p.stdout[-400:])
        assert st["REMOTE_RETURNS"] == st["POSTED_RETURNS"], (
            "a buffer finished off its stream's hub never got back to the "
            "owner's pool\n" + p.stdout[-400:])
        if st["POSTED_RETURNS"] > 0:
            return
    pytest.fail(
        "no echo fiber finished a buffer off its stream's hub in %d runs in a "
        "row (a woken fiber moves only when an idle hub steals it; a single "
        "such run is ~1 in 800): migration with a stream open has stopped\n%s"
        % (_MIGRATE_ATTEMPTS, p.stdout[-400:]), pytrace=False)


@needs_iouring
def test_iouring_loop_multishot_falls_back_with_one_warning():
    # When no hub can register a buffer ring, every multishot stream runs
    # single-shot -- the same bytes -- and the run says so: once on stderr, and
    # in the stats.  Driven on any kernel by sending the registration form THIS
    # kernel refuses: resv[0] = 1 on a correct kernel, or the plain form with
    # the workaround off on an inverted-check one.
    plain, resv0 = kernel_pbuf_ring_errno(), kernel_pbuf_ring_errno(resv0=1)
    if plain == 0:
        knob = "1"
    elif resv0 == 0:
        knob = "0"
    else:
        pytest.skip("this kernel refuses both registration forms (%s, %s): "
                    "test_iouring_loop_echo_survives_fiber_migration[1] "
                    "covers its fallback" % (plain, resv0))
    p, st = _run_echo_migrate("1", STACKWEAVE_IOURING_PBUF_RESV_QUIRK=knob)
    assert st["MS_OPENS"] == 0 and st["MS_FALLBACKS"] == 48, (
        "a stream armed multishot with no buffer ring\n" + p.stdout[-400:])
    assert st["MS_PBUF_ERRNO"] == errno.EINVAL, p.stdout[-400:]
    assert st["PBUF_RESV_QUIRK"] == 0, p.stdout[-400:]
    assert p.stderr.count("provided buffer ring could not be registered") == 1, (
        "expected exactly one capability-degrade warning\n" + p.stderr[-800:])


@needs_iouring
def test_kernel_accepts_one_buffer_ring_registration_form():
    # The workaround's premise.  A kernel accepts the plain registration (zeroed
    # reserved words) or, with the check inverted, the one with resv[0] = 1 --
    # never both, else the retry would not be harmless; and every kernel that
    # needs the second is a 6.8 one, else the runtime's gate misses it.
    plain, resv0 = kernel_pbuf_ring_errno(), kernel_pbuf_ring_errno(resv0=1)
    if plain is None:
        pytest.skip("io_uring cannot be set up here")
    assert not (plain == 0 and resv0 == 0), (
        "kernel %s accepts a nonzero reserved word" % platform.release())
    if plain != 0 and resv0 != 0:
        pytest.skip("this kernel (%s) refuses both forms (%d, %d): no "
                    "provided buffer rings" % (platform.release(), plain, resv0))
    if plain == 0:
        assert resv0 == errno.EINVAL, resv0
    else:
        assert plain == errno.EINVAL and platform.release().startswith("6.8."), (
            "kernel %s has the inverted reserved-word check, but the runtime's "
            "workaround only retries on 6.8 kernels: widen the gate in "
            "src/runloom_c/io_uring_l_pbuf.c.inc" % platform.release())


# --------------------------------------------------------------------------
# 2. Python-handler serve under the io_uring loop: drives the ring recv/send
#    proactor ops through a Python handler (different code path than all-C).
# --------------------------------------------------------------------------
_PYHANDLER = r'''
import sys, struct; sys.path.insert(0, "src")
import stackweave, stackweave_c as rc
from stackweave.sync import WaitGroup
N = 40
got = [None] * N
def main():
    def handler(conn):
        try:
            d = conn.recv(8)
            if d: conn.send_all(d)
        finally:
            conn.close()
    port, lst = rc.serve("127.0.0.1", 0, handler, 2)
    wg = WaitGroup(); wg.add(N)
    def client(i):
        try:
            c = rc.TCPConn.connect("127.0.0.1", port)
            c.send_all(struct.pack(">Q", i)); got[i] = c.recv(8); c.close()
        finally:
            wg.done()
    for i in range(N):
        rc.mn_fiber(lambda i=i: client(i))
    wg.wait()
    for ln in lst: ln.close()
stackweave.run(4, main)
sys.stdout.write("PYH_OK %d\n" % sum(1 for i in range(N) if got[i] == struct.pack(">Q", i)))
'''


@needs_iouring
def test_iouring_loop_python_handler():
    p = _run(_PYHANDLER + IOURING_LOOP_TRAILER, {})
    assert p.returncode == 0, (p.stdout[-400:], p.stderr[-1200:])
    assert "PYH_OK 40" in p.stdout, (p.stdout[-400:], p.stderr[-800:])
    assert_iouring_loop_ran(p)


# --------------------------------------------------------------------------
# 3. repeated mn_init/mn_run/mn_fini cycles under the io_uring loop: drives the
#    per-hub ring CREATE on init AND DESTROY on teardown (hub_main L219-236).
# --------------------------------------------------------------------------
_TEARDOWN = r'''
import sys, struct; sys.path.insert(0, "src")
import stackweave, stackweave_c as rc
from stackweave.sync import WaitGroup
def one_round():
    got = {}
    def main():
        port, lst = rc.serve("127.0.0.1", 0, None, 2)
        wg = WaitGroup(); wg.add(8)
        def cl(i):
            try:
                c = rc.TCPConn.connect("127.0.0.1", port); c.send_all(struct.pack(">Q", i))
                got[i] = c.recv(8); c.close()
            finally:
                wg.done()
        for i in range(8): rc.mn_fiber(lambda i=i: cl(i))
        wg.wait()
        for ln in lst: ln.close()
    stackweave.run(4, main)         # each run() creates + tears down per-hub rings
    return sum(1 for v in got.values() if v)
total = 0
for _ in range(4):
    total += one_round()
sys.stdout.write("TEARDOWN_OK %d\n" % total)
'''


@needs_iouring
def test_iouring_loop_ring_create_destroy_cycles():
    p = _run(_TEARDOWN + IOURING_LOOP_TRAILER, {})
    assert p.returncode == 0, (p.stdout[-400:], p.stderr[-1500:])
    assert "TEARDOWN_OK 32" in p.stdout, (p.stdout[-400:], p.stderr[-1000:])
    assert_iouring_loop_ran(p)


# --------------------------------------------------------------------------
# 4. cancel a fiber parked on an in-flight io_uring op: drives the io_uring
#    ASYNC_CANCEL + the cancel_g pool-relock path under the loop backend.
# --------------------------------------------------------------------------
_CANCEL = r'''
import sys; sys.path.insert(0, "src")
import stackweave, stackweave_c as rc
res = {}
def main():
    # a fiber parks reading a socketpair that never receives; another cancels it
    import socket
    a, b = socket.socketpair()
    a.setblocking(False)
    hold = {}
    def reader():
        # mn_fiber returns None, so the reader records its OWN g handle for the
        # canceller.  park on the fd via wait_fd (under the io_uring loop this
        # routes through the ring); cancel_wait_fd must wake it CANCELLED, not hang
        hold["g"] = rc.current_g()
        res["rv"] = rc.wait_fd(a.fileno(), 1, -1)
    rc.mn_fiber(reader)
    while "g" not in hold:
        rc.sched_yield()
    # hold["g"] is set BEFORE wait_fd commits the netpoll park, so we must not
    # cancel until the park is actually registered -- otherwise the cancel is a
    # no-op and the reader is stranded.  Poll the real counter (netpoll_parked
    # rises to 1 once the reader's wait_fd lands on the ring) instead of guessing
    # with a sleep that load can outrun.  The cap only bounds a hang.
    i = 0
    while rc.stats()["netpoll_parked"] < 1 and i < 1000000:
        rc.sched_yield()
        i += 1
    res["woke"] = hold["g"].cancel_wait_fd()
    # let the woken reader record res["rv"] before we tear the fd down; poll the
    # park draining back out rather than sleeping a fixed amount.
    i = 0
    while "rv" not in res and i < 1000000:
        rc.sched_yield()
        i += 1
    try:
        rc.netpoll_unregister(a.fileno())
    except Exception:
        pass
    a.close(); b.close()
stackweave.run(2, main)
sys.stdout.write("CANCEL rv=%r woke=%r\n" % (res.get("rv"), res.get("woke")))
'''


@needs_iouring
def test_iouring_loop_cancel_parked_fiber():
    p = _run(_CANCEL + IOURING_LOOP_TRAILER, {})
    assert p.returncode == 0, (p.stdout[-400:], p.stderr[-1500:])
    # the parked reader must have been woken (cancelled), not stranded
    assert "CANCEL rv=" in p.stdout and "woke=True" in p.stdout, (
        p.stdout[-400:], p.stderr[-800:])
    assert_iouring_loop_ran(p)


# --------------------------------------------------------------------------
# 5. file I/O under the io_uring loop: drives the ring file_read/file_write +
#    the global-ring eventfd drain.
# --------------------------------------------------------------------------
_FILEIO = r'''
import sys, os, tempfile; sys.path.insert(0, "src")
import stackweave, stackweave_c as rc
ok = bytearray(24)
def main():
    def one(i):
        fd, path = tempfile.mkstemp()
        try:
            rc.file_write(fd, b"u" * 4096, 0)
            buf = bytearray(4096)
            if rc.file_read(fd, buf, 4096, 0) == 4096 and buf == bytearray(b"u" * 4096):
                ok[i] = 1
        finally:
            os.close(fd); os.unlink(path)
    for i in range(24):
        rc.mn_fiber(lambda i=i: one(i))
stackweave.run(4, main)
sys.stdout.write("FILEIO_OK %d\n" % sum(ok))
'''


@needs_iouring
@pytest.mark.skipif(not hasattr(__import__("stackweave_c"), "file_read"),
                    reason="file_read not built")
def test_iouring_loop_file_io():
    p = _run(_FILEIO + IOURING_LOOP_TRAILER, {})
    assert p.returncode == 0, (p.stdout[-400:], p.stderr[-1200:])
    assert "FILEIO_OK 24" in p.stdout, (p.stdout[-400:], p.stderr[-800:])
    assert_iouring_loop_ran(p)


# --------------------------------------------------------------------------
# 6. netpoll TIMED parks expire under the loop backend.  The hub blocks in its
#    ring, not in the epoll pump, and the pump is where a wait_fd deadline is
#    both waited-for (the pump clamps its epoll_wait to the deadline heap) and
#    fired (its post-wait sweep).  Without the ring branch doing the same, a
#    timed park on an fd that never becomes ready -- a plain wait_fd(timeout)
#    on a quiet pipe, or context.WithTimeout's deadline_waker on the always-
#    quiet wake fd -- produced no epoll edge and so never timed out: the
#    fiber hung until unrelated readiness woke the hub.  Both shapes here; the
#    in-child watchdog turns the hang into a FAIL (a sleeper does wake the
#    hub: the ring wait is clamped to the sleep heap, only not to netpoll's).
# --------------------------------------------------------------------------
_TIMED_PARK = r'''
import os, sys, time; sys.path.insert(0, "src")
import stackweave, stackweave_c as rc
import stackweave.context as ctx
READ = 1
out = {}
def watchdog():
    stackweave.sleep(3.0)
    if "done" not in out:
        sys.stdout.write("HUNG %r\n" % (out,)); sys.stdout.flush(); os._exit(3)
def main():
    rc.mn_fiber(watchdog)
    r, w = os.pipe()                       # no writer: readiness never comes
    t0 = time.monotonic()
    rv = rc.wait_fd(r, READ, 50)
    out["wait_fd"] = (rv, time.monotonic() - t0)
    rc.netpoll_unregister(r); os.close(r); os.close(w)
    c, _cancel = ctx.WithTimeout(ctx.Background(), 0.02)
    stackweave.sleep(0.2)
    out["ctx_err"] = c.err()
    out["done"] = 1
stackweave.run(2, main)      # run() tears the hub rings down, which folds the loop-waits stat
rv, took = out["wait_fd"]
print("TIMED_PARK rv=%d took=%.3f ctx=%r" % (rv, took, out["ctx_err"]))
'''


@needs_iouring
def test_iouring_loop_timed_park_expires():
    p = _run(_TIMED_PARK + IOURING_LOOP_TRAILER, {})
    assert p.returncode == 0, (p.stdout[-400:], p.stderr[-1200:])
    m = re.search(r"TIMED_PARK rv=(\d+) took=([\d.]+) ctx=(\S+)", p.stdout)
    assert m is not None, (p.stdout[-400:], p.stderr[-800:])
    rv, took, err = int(m.group(1)), float(m.group(2)), m.group(3)
    assert rv == 0, "wait_fd on a quiet pipe must time out (rv=%d)" % rv
    # 50 ms asked; allow a loaded box, but not the 3 s watchdog or a 500 ms
    # idle tick.  Unfixed, this never returned at all.
    assert 0.045 <= took < 1.0, "wait_fd(50ms) took %.3fs" % took
    assert err != "None", "WithTimeout deadline never fired"
    assert_iouring_loop_ran(p)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
