"""big_100 / 224 -- per-hub epoll cross-hub lost-park.

The Linux netpoll architecture is PER-HUB epoll: each hub owns its own epoll fd
plus a per-hub wake-eventfd, and a socket fd is registered/parked in whatever
hub the parking goroutine lands on.  The exact bug fixed at b46a586 was
*lost-park-across-hubs*: an fd armed on hub A but whose readiness is driven from
a goroutine that lands on hub B, where the wake must route back to A's pump via
A's wake-eventfd.  Nothing else in the campaign names per-hub epoll as the
system under test or parks an fd across hubs.

This program builds many socketpairs.  For each pair it spawns TWO goroutines
(spawned in two separate passes so they fan out across different hubs): a
'parker' that blocks in recv on one end -- arming/parking that fd on whatever
hub it lands on -- and a 'writer' that jitter-sleeps then writes a tagged byte
to the partner end (driven from, with high probability, a DIFFERENT hub).  The
oracle: every parker must wake with the EXACT byte its partner wrote, inside the
watchdog window.  A lost cross-hub wake strands the parker, no byte is observed,
and the watchdog/_dump_parkers fires (readyParked>0 = lost wakeup).

It is meaningful only on Linux-epoll (kqueue/select have no per-hub
epoll), so it SKIPs cleanly off epoll.

Stresses: per-hub epoll fd registration + per-hub wake-eventfd routing under cross-hub fd parking; the lost-park-across-hubs path (fd armed on hub A, readiness/peer-write driven from a goroutine landing on hub B).
"""
import socket
import sys

import harness
import stackweave
import stackweave_c

# One tagged byte travels each socketpair; the parker checks it equals the byte
# its writer partner sent (tag = pair index, low 8 bits -- enough to catch a
# cross-pair / cross-hub mis-route, which is what a lost-park bug looks like).
PAIRS_PER_ROUND = 1     # one rendezvous per round per worker; --rounds scales it


def _make_pairs(n):
    """n connected socketpairs, both ends non-blocking (cooperative recv/send
    park the goroutine under monkey.patch())."""
    pairs = []
    for _ in range(n):
        a, b = socket.socketpair()
        a.setblocking(False)
        b.setblocking(False)
        pairs.append((a, b))
    return pairs


def parker(H, wid, rng, pairs):
    """Block in recv on end-A of this worker's pair; assert the byte that
    arrives is exactly the tag the writer partner will send.  recv parks the
    A-end fd on whatever hub THIS goroutine lands on."""
    a, _b = pairs[wid]
    expect = bytes([wid & 0xFF])
    for _ in H.round_range():
        try:
            # Park on the A-end.  Under per-hub epoll this arms the fd in this
            # goroutine's hub; the wake will be driven from the writer's hub.
            got = a.recv(1)
        except OSError:
            if not H.running():
                break
            continue
        if not got:
            # EOF: only legitimate at teardown (closeables closed).
            if not H.running():
                break
            H.check(False, "parker wid={0} got EOF before tag".format(wid))
            return
        if not H.check(got == expect,
                       "cross-hub wake mis-route wid={0}: got {1!r} want "
                       "{2!r}".format(wid, got, expect)):
            return
        H.op(wid)
        H.task_done(wid)


def writer(H, wid, rng, pairs):
    """Sleep a jittered moment (so the parker is parked first), then write the
    tagged byte to end-B.  Spawned in a separate pass to maximise landing on a
    DIFFERENT hub than its parker -> exercises the cross-hub wake route."""
    _a, b = pairs[wid]
    tag = bytes([wid & 0xFF])
    for _ in H.round_range():
        # Jitter so the parker is reliably parked when the write lands -- the
        # write must then DRIVE the wake (the cross-hub path), not merely fill a
        # buffer the parker drains synchronously.
        H.sleep(0.001 + rng.random() * 0.02)
        if not H.running():
            break
        try:
            b.sendall(tag)
        except OSError:
            if not H.running():
                break


def worker(H, wid, rng, pairs):
    # Unused: spawning is split into two explicit passes in body() so parkers
    # and writers fan out across hubs independently.  Kept for run_pool's
    # worker(H, wid, rng, *extra) signature when invoked directly is not used.
    parker(H, wid, rng, pairs)


def setup(H):
    n = H.funcs
    pairs = _make_pairs(n)
    for a, b in pairs:
        H.register_close(a)
        H.register_close(b)
    H.state = pairs


def body(H):
    pairs = H.state
    n = len(pairs)
    # Pass 1: all parkers.  Pass 2: all writers.  Two separate run_pool calls
    # spawn the two roles in distinct waves, so a parker[wid] and writer[wid]
    # very likely land on different hubs -> the byte writer[wid] sends must wake
    # parker[wid] across hubs (the lost-park-across-hubs path).
    H.run_pool(n, parker, pairs)
    H.run_pool(n, writer, pairs)


def post(H):
    H.check(H.total_ops() > 0,
            "no cross-hub wakes completed (every parker stranded?)")
    H.log("cross_hub_wakes={0}".format(H.total_ops()))


if __name__ == "__main__":
    if stackweave_c.netpoll_backend() != "epoll":
        print("SKIP: per-hub epoll is meaningless off Linux-epoll "
              "(backend={0})".format(stackweave_c.netpoll_backend()))
        sys.exit(0)
    harness.main(
        "p224_perhub_epoll_cross_hub_wake", body, setup=setup, post=post,
        default_funcs=2000,
        describe="cross-hub fd park/wake under per-hub epoll: every parker "
                 "must wake with its writer's exact tagged byte")
