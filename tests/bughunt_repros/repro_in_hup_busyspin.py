# Repro: the READ-side twin of repro_out_busyspin.py, which is unfixed.
#
# After a READ park completes, the fd stays LEVEL-armed for EPOLLIN with no
# waiter -- exactly as it stays armed for EPOLLOUT in the OUT repro. For OUT
# the pump notices ("Level-triggered EPOLLOUT with no writer parked would
# otherwise re-fire on every pump iteration and busy-spin the idle hub at
# 100% CPU") and calls runloom_netpoll_disarm_out. There is no disarm_in:
# grep the tree, disarm_out is the only one.
#
# Measured both ways (see the two cases below): an unparked level-armed
# EPOLLIN busy-spins whether the socket merely has unread data OR the peer
# has hung up. The difference is not whether it spins but whether it STOPS.
# Unread data is consumed by the next reader, so a healthy server's spin is
# bounded by how long until someone parks on that fd again. A hangup is
# terminal -- EPOLLIN|EPOLLHUP|EPOLLERR is asserted forever, no read will
# ever clear it, and if the fd stays open with nobody parked, every
# epoll_wait returns instantly for the remaining life of the process.
#
# So the fix is the exact mirror of disarm_out, not a hangup special case:
# on a dispatch miss carrying READ, drop the IN arm and let the next
# wait_fd(READ) re-arm and consume the pending-wake bit -- which is
# precisely the race-safe mechanism disarm_out already documents.
#
# This is not hypothetical. soupchan on ovh1 was measured on 2026-08-21 with
# three such fds (44, 59, 95) registered events=0x2019 in epoll fd 9,
# 1,337,592 epoll_wait calls in 10 seconds, 8 hub threads at ~85% CPU each,
# load average 8.09 on 8 cores. The process had burned 6d 1h of CPU in 48h
# of wall clock. Those fds were absent from /proc/net/tcp entirely: fully
# torn down at the TCP layer, fd still open, arm still in place.
#
# Exit 1 = bug present.
import os, socket, sys
import stackweave
import stackweave_c as rc

READ, WRITE = 1, 2
res = {}

# "hangup"  -- peer closed: EPOLLIN|EPOLLHUP|EPOLLRDHUP forever, never heals.
# "unread"  -- peer alive, data left in the buffer: EPOLLIN with no parker.
CASE = sys.argv[1] if len(sys.argv) > 1 else "hangup"


def cpu_seconds():
    t = os.times()
    return t.elapsed, t.user + t.system


def main():
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)

    # One READ park that actually completes, so `a` ends up armed for EPOLLIN
    # level-triggered with no waiter left behind -- the same starting state
    # the OUT repro sets up with its single WRITE park.
    b.send(b"x")
    res["r"] = rc.wait_fd(a.fileno(), READ, 2000)
    a.recv(64)

    if CASE == "hangup":
        # The peer goes away. `a` is now permanently EPOLLIN|EPOLLHUP|
        # EPOLLRDHUP and nothing will ever clear it. Critically `a` is NOT
        # closed: closing it would let the kernel drop the epoll entry by
        # itself, which is exactly what runloom_netpoll_unregister relies on
        # ("No syscall; the kernel auto-clears its epoll entry when the last
        # fd reference closes"). The production case is the one where that
        # assumption does not get to apply -- the fd stayed open.
        b.close()
    else:
        # Peer alive, just unread. Same unparked level arm, same spin, but
        # this one ends as soon as anybody reads it.
        b.send(b"leftover")
    res["keep_b"] = b

    # A SECOND fd with a fiber genuinely parked on it, for the whole idle
    # window. Without this the repro passes vacuously: with no netpoll parker
    # anywhere the scheduler never pumps netpoll at all, so the stale arm is
    # never observed and the fd sits there costing nothing. disarm_out's own
    # comment states the precondition -- the spin lasts "for as long as the fd
    # stays open with any fiber netpoll-parked". A server always has one (an
    # accept loop, another keepalive connection), which is why production
    # spins and a bare two-socket unit test does not.
    c, d = socket.socketpair()
    c.setblocking(False)
    d.setblocking(False)
    res["keeper"] = (c, d)

    def keeper():
        rc.wait_fd(c.fileno(), READ, 4000)   # parks for the whole measurement

    stackweave.fiber(keeper)
    stackweave.yield_now()                      # let it reach the park

    e0, c0 = cpu_seconds()
    stackweave.sleep(3.0)          # runtime is idle: expect ~0 CPU
    e1, c1 = cpu_seconds()
    res["idle_wall"] = e1 - e0
    res["idle_cpu"] = c1 - c0
    res["socks"] = (a,)


stackweave.run(1, main)
print("read park result:", res["r"])
print("idle wall=%.2fs cpu=%.2fs" % (res["idle_wall"], res["idle_cpu"]))
if res["idle_cpu"] > 0.5 * res["idle_wall"]:
    print("BUG: idle pump busy-spins on the stale IN level arm after peer hangup")
    sys.exit(1)
print("OK")
sys.exit(0)
