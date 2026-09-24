"""Adversarial coverage suite for src/runloom_c/mn_sched_runq.c.inc.

WHAT THIS FRAGMENT IS
---------------------
mn_sched_runq.c.inc holds the *global stealable run-queue* (push/pull) plus
runloom_use_global_runq(), which says whether that queue is in use:

  * runloom_mn_global_runq_push
  * runloom_mn_global_runq_pull
  * runloom_use_global_runq  (== runloom_get_per_g_tstate_mode(): set by mn_init,
    cleared by mn_fini)

Cross-hub migration is always on, so every M:N run routes woken gs through the
global run-queue: wake_g pushes (mn_api.c.inc), and idle hubs pull in
hub_main's empty-local / empty-deque path.

WHAT THIS SUITE ASSERTS
-----------------------
A subprocess runs a real cross-hub channel + cross-hub fd-park workload to
completion under M:N, exits 0, and prints a marker -- so gcov counters flush and
we assert on stdout + returncode, never on a crash.  Every woken g in it travels
push -> pull, so a lost, duplicated or stranded runq entry shows up as a missing
value/byte or a hang.
"""
import os
import subprocess
import sys

import pytest

from adv_util import hang_guard, needs_free_threading

FT = needs_free_threading()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

# A self-contained child program. It runs a workload whose wakeups route through
# the global runq push/pull:
#   * a cross-hub unbuffered channel rendezvous (consumer parks on hub A, sender
#     on hub B wakes it -> runloom_mn_wake_g),
#   * a cross-hub socketpair fd park (reader parks in netpoll on one hub, writer
#     on another wakes it).
# It asserts every value/byte arrived (no lost/dup/stranded wake) and prints
# CHILD_OK.
_CHILD = r'''
import os, sys, socket
sys.path.insert(0, "src")
import stackweave
import stackweave_c as rc
from stackweave.sync import WaitGroup

HUBS = int(sys.argv[1])
PAIRS = 64

# Per-consumer single-writer slots: consumer i is the ONLY writer of recv_val[i]
# (race-free with the GIL off). We verify the MULTISET of received values equals
# {0..PAIRS-1} -- i.e. every sent value was delivered exactly once, none lost or
# duplicated -- rather than pairing-by-index (an unbuffered chan pairs sends and
# recvs in arbitrary order, so consumer i need not get value i).
recv_val = [None] * PAIRS
recv_ok = bytearray(PAIRS)
fd_got = bytearray(PAIRS)

def main():
    wg = WaitGroup()

    # --- cross-hub channel rendezvous storm ---
    ch = rc.Chan(0)           # unbuffered: forces a real park+wake handoff
    wg.add(PAIRS)
    def consumer(i):
        try:
            v, ok = ch.recv()
            recv_val[i] = v       # sole writer of slot i
            recv_ok[i] = 1 if ok else 0
        finally:
            wg.done()
    for i in range(PAIRS):
        rc.mn_fiber(lambda i=i: consumer(i))
    # senders on (potentially) other hubs wake the parked consumers
    for i in range(PAIRS):
        rc.mn_fiber(lambda i=i: ch.send(i))
    wg.wait()

    # --- cross-hub fd park/wake storm (netpoll wake path) ---
    wg2 = WaitGroup()
    wg2.add(PAIRS)
    socks = []
    def fd_pair(i):
        a, b = socket.socketpair()
        a.setblocking(False); b.setblocking(False)
        socks.append((a, b))
        def reader():
            try:
                buf = bytearray(1)
                rc.tcp_recv(a.fileno(), buf, 1)   # park in netpoll
                if buf[0] == (i & 0x7f):
                    fd_got[i] = 1
            finally:
                rc.netpoll_unregister(a.fileno())
                wg2.done()
        def writer():
            rc.sched_yield()
            rc.tcp_send(b.fileno(), bytes([i & 0x7f]))
        rc.mn_fiber(reader)
        rc.mn_fiber(writer)
    for i in range(PAIRS):
        fd_pair(i)
    wg2.wait()
    for a, b in socks:
        try: rc.netpoll_unregister(b.fileno())
        except Exception: pass
        a.close(); b.close()

stackweave.run(HUBS, main)

assert sum(recv_ok) == PAIRS, "channel recv not ok: %d/%d" % (sum(recv_ok), PAIRS)
assert sorted(recv_val) == list(range(PAIRS)), (
    "channel wake lost/dup: got multiset %s" % sorted(recv_val))
assert sum(fd_got) == PAIRS, "lost fd wake(s): %d/%d" % (sum(fd_got), PAIRS)
print("CHILD_OK", sum(recv_ok), sum(fd_got))
'''


def _run_child(hubs, timeout=60):
    env = dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src")
    return subprocess.run(
        [PY, "-c", _CHILD, str(hubs)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout)


# --------------------------------------------------------------------------
# Cross-hub channel + fd wakes all delivered through the global run-queue.
# Multi-hub so cross-hub wake_g is genuinely exercised.
# --------------------------------------------------------------------------
@pytest.mark.skipif(not FT, reason="M:N needs GIL-disabled build")
def test_cross_hub_wakes_via_global_runq():
    with hang_guard(70, "global runq cross-hub wakes"):
        p = _run_child(hubs=4)
    assert p.returncode == 0, (
        "cross-hub wake child crashed (rc=%d).\nstderr=%s"
        % (p.returncode, p.stderr[-2000:]))
    assert "CHILD_OK" in p.stdout, (
        "the global runq did not deliver every cross-hub wake "
        "(channel + fd) -> work stranded.\nout=%s\nerr=%s"
        % (p.stdout, p.stderr[-1200:]))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
