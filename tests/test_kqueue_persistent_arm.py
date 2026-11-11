"""kqueue: a hub pool's persistent (LEVEL) arm (runloom_kq_register_persist).

Each M:N hub keeps one LEVEL knote per armed direction in its own kqueue and
skips the kevent on a re-park, as epoll does.  These pin the cases that arm can
get wrong, each in its own child (counters and knobs are read once per process):

  * a knote the arm cache no longer records (an fd unregistered while still
    open) is deleted by the hub whose kqueue reports it, not left to fire on
    every kevent (a readable socket's stray knote spun that hub);
  * socket.detach() drops the kernel registration while the socket still owns
    the fd, so no stray knote is left at all;
  * release_if_idle deletes the knotes and forgets the arm, so the same fiber
    re-parking on a reused fd number adds a fresh knote at once;
  * a different fiber parking on a reused fd number re-adds at once, and the
    same fiber is healed by the stale-arm probe (epoll's delay for that case);
  * a failed re-add leaves no record that the fiber armed the fd, so its retry
    adds again instead of skipping;
  * a reader and a writer fiber on one socket each keep the zero-syscall
    re-park (the last parker is kept per direction);
  * a miss on a persistent knote leaves no pending-wake bit for the next
    socket given that fd number.

The fd-reuse cases need the OS to hand the closed number to the next socket
(the lowest free one); each tries up to five times, as another thread opening
an fd in between takes it.
"""
import sys

import pytest

from adv_util import run_python

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("darwin", "freebsd", "openbsd", "netbsd")),
    reason="kqueue backend only")

PRELUDE = r"""
import os, socket, time
import stackweave, stackweave_c
READ, WRITE = 1, 2
def counters():
    st = stackweave_c.stats()
    return {k[len("netpoll_c_"):]: v for k, v in st.items() if k.startswith("netpoll_c_")}
def join(fn):
    done = []
    stackweave.fiber(lambda: (fn(), done.append(1)))
    while not done:
        stackweave.sleep(0.002)
def pair():
    a, b = socket.socketpair()
    a.setblocking(False); b.setblocking(False)
    return a, b
def reused_pair(fdnum):
    # A socketpair whose first end has fd number fdnum, or None.
    c, d = pair()
    if c.fileno() != fdnum:
        c.close(); d.close()
        return None
    return c, d
def arm_read(a, b):
    # Arm READ on a (a persistent knote in this fiber's hub's kqueue).
    b.send(b"x")
    assert stackweave_c.wait_fd(a.fileno(), READ, 2000) & READ
    a.recv(1)
def keep_hubs_polling(secs):
    # A fiber parked on a quiet socket: with anything parked, every idle hub
    # polls its own kqueue instead of sleeping on its condvar.
    q, q2 = pair()
    stackweave.fiber(lambda: (stackweave_c.wait_fd(q.fileno(), READ, int(secs * 1000)),
                              q.close(), q2.close()))
def attempts(scenario):
    for _ in range(5):
        if scenario():
            print("R reused=1")
            return
    print("R reused=0")
"""


def _run(body, env, timeout=60):
    p = run_python(PRELUDE + body, env=env, timeout=timeout)
    assert p.returncode == 0 and "DONE" in p.stdout, (
        p.returncode, p.stdout[-3000:], p.stderr[-4000:])
    out = {}
    for line in p.stdout.splitlines():
        if line.startswith("R "):
            k, v = line[2:].split("=", 1)
            out[k] = v
    return out


def _reused(out):
    assert out.get("reused") == "1", (
        "the OS never handed the closed fd number to the next socket in five "
        "tries: %r" % (out,))


def test_a_stray_knote_is_deleted_by_the_kqueue_that_reports_it():
    # READ armed persistently, then the arm cache cleared with the fd still
    # open (what detach() used to leave), then the socket made readable with
    # nobody parked.  Every idle hub is polling its kqueue, so a knote nobody
    # deletes shows up as a stream of dispatch misses.
    body = r"""
def main():
    a, b = pair()
    join(lambda: arm_read(a, b))
    stackweave_c.netpoll_unregister(a.fileno())
    keep_hubs_polling(0.9)
    stackweave.sleep(0.1)
    m0 = counters()["dispatch_miss"]
    b.send(b"unread")
    stackweave.sleep(0.4)
    print("R misses=%d" % (counters()["dispatch_miss"] - m0))
stackweave.run(2, main)
print("DONE")
"""
    out = _run(body, {"STACKWEAVE_NETPOLL_COUNTERS": "1"})
    assert int(out["misses"]) < 20, out


def test_detach_drops_the_kernel_registration():
    # With the monkey hooks, detach() deletes the knote while the socket still
    # owns the fd: nothing is left for a hub to report, not even one miss.
    body = r"""
from stackweave import monkey
monkey.patch()
def main():
    a, b = pair()
    join(lambda: arm_read(a, b))
    fd = a.detach()
    keep_hubs_polling(0.9)
    stackweave.sleep(0.1)
    m0 = counters()["dispatch_miss"]
    b.send(b"unread")
    stackweave.sleep(0.4)
    print("R misses=%d" % (counters()["dispatch_miss"] - m0))
    # The fd still works when waited on again (the next park adds it back).
    s = socket.socket(fileno=fd)
    s.setblocking(False)
    def again():
        print("R again=%d" % stackweave_c.wait_fd(s.fileno(), READ, 2000))
    join(again)
stackweave.run(2, main)
print("DONE")
"""
    out = _run(body, {"STACKWEAVE_NETPOLL_COUNTERS": "1"})
    assert int(out["misses"]) == 0, out
    assert int(out["again"]) & 1, out


def test_release_if_idle_forgets_the_arm_so_a_reused_number_re_adds():
    # The same fiber re-parks in the same direction on a reused fd number,
    # the case only the stale-arm probe heals.  After release_if_idle the arm
    # is forgotten, so the park adds a fresh knote and wakes at once; the
    # probe is set to 3 s, so a skip would show as a 3 s wait.
    body = r"""
def main():
    def f():
        def scenario():
            a, b = pair()
            fdnum = a.fileno()
            arm_read(a, b)
            armed0 = stackweave_c.stats()["netpoll_fd_armed"]
            stackweave_c.netpoll_release_if_idle(fdnum)
            print("R armed_drop=%d" % (armed0 - stackweave_c.stats()["netpoll_fd_armed"]))
            a.close(); b.close()               # raw close: no unregister hook
            p = reused_pair(fdnum)
            if p is None:
                return False
            c, d = p
            d.send(b"y")
            t0 = time.monotonic()
            print("R rv=%d" % stackweave_c.wait_fd(c.fileno(), READ, 6000))
            print("R ms=%d" % ((time.monotonic() - t0) * 1000))
            return True
        attempts(scenario)
    join(f)
stackweave.run(2, main)
print("DONE")
"""
    out = _run(body, {"STACKWEAVE_STALE_ARM_PROBE_MS": "3000"})
    _reused(out)
    assert int(out["armed_drop"]) == 1, out
    assert int(out["rv"]) & 1, out
    assert int(out["ms"]) < 1500, out


def test_a_different_fiber_on_a_reused_fd_number_re_adds_at_once():
    # With the probe off, only the per-direction last-parker check can put
    # back the knote the raw close dropped.
    body = r"""
def main():
    def scenario():
        state = {}
        def f():
            a, b = pair()
            state["fd"] = a.fileno()
            arm_read(a, b)
            a.close(); b.close()
        join(f)
        p = reused_pair(state["fd"])
        if p is None:
            return False
        c, d = p
        def g():
            d.send(b"y")
            print("R rv=%d" % stackweave_c.wait_fd(c.fileno(), READ, 1500))
        join(g)
        return True
    attempts(scenario)
stackweave.run(2, main)
print("DONE")
"""
    out = _run(body, {"STACKWEAVE_STALE_ARM_PROBE_MS": "0"})
    _reused(out)
    assert int(out["rv"]) & 1, out


def test_the_same_fiber_on_a_reused_fd_number_is_healed_by_the_probe():
    # The same fiber, the same direction: the park skips on the cached arm,
    # and the stale-arm probe re-adds the knote and counts a heal (epoll's
    # delay for this case).
    body = r"""
def main():
    def f():
        def scenario():
            a, b = pair()
            fdnum = a.fileno()
            arm_read(a, b)
            a.close(); b.close()
            p = reused_pair(fdnum)
            if p is None:
                return False
            c, d = p
            h0 = stackweave_c.stats()["stale_arm_heals"]
            d.send(b"y")
            print("R rv=%d" % stackweave_c.wait_fd(c.fileno(), READ, 5000))
            print("R heals=%d" % (stackweave_c.stats()["stale_arm_heals"] - h0))
            return True
        attempts(scenario)
    join(f)
stackweave.run(2, main)
print("DONE")
"""
    out = _run(body, {"STACKWEAVE_STALE_ARM_PROBE_MS": "100"})
    _reused(out)
    assert int(out["rv"]) & 1, out
    assert int(out["heals"]) >= 1, out


def test_a_failed_re_add_is_retried_by_the_next_park():
    # The second registration kevent of the process fails (nth:2, ENOMEM): it
    # is the re-add a different fiber makes on a reused fd number.  That
    # fiber's retry must add again, not skip on an arm its failed call
    # recorded.  One attempt only: the fault is counted per process.
    body = r"""
def main():
    def scenario():
        state = {}
        def f():
            a, b = pair()
            state["fd"] = a.fileno()
            arm_read(a, b)
            a.close(); b.close()
        join(f)
        p = reused_pair(state["fd"])
        if p is None:
            return False
        c, d = p
        def g():
            try:
                stackweave_c.wait_fd(c.fileno(), READ, 1500)
                print("R first=ok")
            except OSError as e:
                print("R first=%d" % e.errno)
            d.send(b"y")
            print("R rv=%d" % stackweave_c.wait_fd(c.fileno(), READ, 1500))
        join(g)
        return True
    print("R reused=%d" % (1 if scenario() else 0))
stackweave.run(2, main)
print("DONE")
"""
    out = _run(body, {"STACKWEAVE_STALE_ARM_PROBE_MS": "0",
                      "STACKWEAVE_FAULT_KQUEUE_CTL": "nth:2:12"})
    _reused(out)
    assert out["first"] == "12", out          # the injected ENOMEM
    assert int(out["rv"]) & 1, out


def test_a_reader_and_a_writer_fiber_keep_the_zero_syscall_re_park():
    # One socket, a reader fiber and a writer fiber.  With one last-parker
    # record per fd, each park by the other fiber re-added the arm, so the
    # reader's re-parks never skipped.
    body = r"""
N = 200
def main():
    a, b = pair()
    got = []
    def reader():
        for _ in range(N):
            assert stackweave_c.wait_fd(a.fileno(), READ, 5000) & READ
            got.append(a.recv(1))
    def writer():
        for _ in range(N):
            assert stackweave_c.wait_fd(a.fileno(), WRITE, 5000) & WRITE
            a.send(b"w")
            stackweave.sleep(0.0005)
    stackweave.fiber(reader)
    stackweave.fiber(writer)
    stackweave.sleep(0.05)
    c0 = counters()
    for _ in range(N):
        b.send(b"r")
        stackweave.sleep(0.0005)
    while len(got) < N:
        stackweave.sleep(0.002)
    c1 = counters()
    print("R skip=%d" % (c1["reg_skip"] - c0["reg_skip"]))
    print("R ctl=%d" % (c1["reg_ctl"] - c0["reg_ctl"]))
stackweave.run(2, main)
print("DONE")
"""
    out = _run(body, {"STACKWEAVE_NETPOLL_COUNTERS": "1"})
    # Every reader re-park after the first skips (N - 1; one record per fd
    # gave about N / 2).  The writer's WRITE parks re-add: an idle WRITE knote
    # is dropped at its first miss.
    assert int(out["skip"]) >= 180, out


def test_a_persistent_miss_leaves_no_pending_bit_for_the_next_socket():
    # Data arrives with nobody parked on a persistent READ arm (a miss).  A
    # pending-wake bit set then would outlive the raw close and wake the next
    # socket given that number, which has no data.
    body = r"""
def main():
    def f():
        def scenario():
            a, b = pair()
            fdnum = a.fileno()
            arm_read(a, b)
            b.send(b"z")                       # nobody parked: a miss
            stackweave.sleep(0.05)
            a.close(); b.close()
            p = reused_pair(fdnum)
            if p is None:
                return False
            c, d = p
            t0 = time.monotonic()
            print("R rv=%d" % stackweave_c.wait_fd(c.fileno(), READ, 300))
            print("R ms=%d" % ((time.monotonic() - t0) * 1000))
            return True
        keep_hubs_polling(2.0)
        attempts(scenario)
    join(f)
stackweave.run(2, main)
print("DONE")
"""
    out = _run(body, {})
    _reused(out)
    assert int(out["rv"]) == 0, out
    assert int(out["ms"]) >= 250, out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
