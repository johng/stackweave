"""M:N fibers take their frame-stack chunk from their hub's pool.

Each M:N fiber has its own thread state.  Before it pooled, CPython mapped a
fresh 16 KB chunk at each fiber's first Python call and unmapped it when the
state was deleted: on macOS that was ~60% of a hub's busy time running no-op
fibers.  Now a Python fiber takes a chunk from the hub's pool when it starts
(the pool the single-thread scheduler uses), and the hub gives the chunks back
when the fiber ends.  stats() counts each Python fiber once, as
`fiber_chunks_reused` (from the pool) or `fiber_chunks_mapped` (the pool was
empty, so CPython mapped one); each hub adds its counts when it exits, so they
are complete once run() returns.
"""
import functools
import sys

import pytest

import stackweave
import stackweave_c
from stackweave.sync import WaitGroup

from adv_util import run_python

HUBS = 4


def _counts():
    s = stackweave_c.stats()
    return s["fiber_chunks_reused"], s["fiber_chunks_mapped"]


def _run_counted(main):
    r0, m0 = _counts()
    stackweave.run(HUBS, main)
    r1, m1 = _counts()
    return r1 - r0, m1 - m0


def test_fork_join_fibers_reuse_pooled_chunks():
    spawners, rounds, width = 4, 25, 100

    def main():
        def spawner():
            for _ in range(rounds):
                wg = WaitGroup()
                wg.add(width)

                def worker():
                    wg.done()

                for _ in range(width):
                    stackweave.fiber(worker)
                wg.wait()

        for _ in range(spawners):
            stackweave.fiber(spawner)

    reused, mapped = _run_counted(main)
    fibers = spawners * rounds * width + spawners + 1   # + the spawners and main
    # Every Python fiber is counted, once.
    assert reused + mapped == fibers, (reused, mapped, fibers)
    # Measured ~92% on macOS and Linux arm64.  A hub reuses only what fibers
    # finished on it have released (past the 128-chunk grace ring), and a
    # fiber that parks may finish on another hub, so the margin is wide.
    assert reused >= fibers // 2, (reused, mapped, fibers)


# A wide() frame holds 300 locals (~2.5 KB), so 40 of them span several 16 KB
# chunks while using little C stack: a fiber overflows its 512 KB stack at
# under 200 frames, each Python call taking a C frame there.  The shallow
# fibers start in joined waves, so most start after others have finished.
_DEEP_THEN_SHALLOW = r'''
import stackweave, stackweave_c as rc
from stackweave.sync import WaitGroup
DEPTH, DEEP, WAVES, WIDTH = 40, 8, 100, 20
ns = {}
exec("def wide(n):\n"
     "    %s = [n] * 300\n"
     "    return 0 if n == 0 else 1 + wide(n - 1)\n"
     % ", ".join("a%d" % i for i in range(300)), ns)
wide = ns["wide"]
def recurse(n):
    return 0 if n == 0 else 1 + recurse(n - 1)
results = []
def main():
    wg = WaitGroup(); wg.add(DEEP)
    def deep_fiber():
        try:
            results.append(wide(DEPTH))
        finally:
            wg.done()
    for _ in range(DEEP):
        stackweave.fiber(deep_fiber)
    wg.wait()
    for w in range(WAVES):
        wg2 = WaitGroup(); wg2.add(WIDTH)
        def shallow_fiber(i):
            try:
                results.append(recurse(i % 50))
            finally:
                wg2.done()
        for i in range(WIDTH):
            stackweave.fiber(shallow_fiber, w * WIDTH + i)
        wg2.wait()
s0 = rc.stats()
stackweave.run(4, main)
s1 = rc.stats()
want = sorted([DEPTH] * DEEP + [i % 50 for i in range(WAVES * WIDTH)])
print("DEEP exact=%d reused=%d" % (sorted(results) == want,
      s1["fiber_chunks_reused"] - s0["fiber_chunks_reused"]))
'''


def test_a_chunk_from_a_deep_fiber_serves_later_fibers():
    # A deep recursion overflows its first chunk into more; when the fiber
    # ends, all of them (CPython's cached spare too) go back to the pool, and
    # later fibers start on them.  Every result must still be exact.  The
    # grace ring is off, so a finished fiber's chunks are reusable at once.
    p = run_python(_DEEP_THEN_SHALLOW, timeout=120,
                   env={"STACKWEAVE_CHUNK_GRACE": "0"})
    assert p.returncode == 0, (p.stdout[-400:], p.stderr[-1600:])
    line = [l for l in p.stdout.splitlines() if l.startswith("DEEP ")]
    assert line, (p.stdout[-400:], p.stderr[-800:])
    fields = dict(kv.split("=") for kv in line[0][5:].split())
    assert fields["exact"] == "1", line[0]
    assert int(fields["reused"]) > 0, line[0]


# C all the way down: the fiber parks in sched_sleep before any Python frame.
_NAP = functools.partial(stackweave_c.sched_sleep, 0.01)


@pytest.mark.parametrize("spawn", ["mn_fiber", "fiber"])
def test_a_fiber_that_parks_in_c_first_is_counted_once(spawn):
    # The chunk is installed when a fiber starts, not at a resume: installed
    # at each resume until the fiber pushed a frame, a fiber like this one
    # was counted (and offered a chunk) again every time it woke.
    # stackweave.fiber wraps the first few spawns of a callable in Python
    # while the stack auto-sizer samples it, and spawns the rest bare.
    n = 200

    def main():
        go = stackweave_c.mn_fiber if spawn == "mn_fiber" else stackweave.fiber
        for _ in range(n):
            go(_NAP)

    reused, mapped = _run_counted(main)
    assert reused + mapped == n + 1, (reused, mapped, n + 1)   # + main


_C_ONLY = r'''
import stackweave, stackweave_c as rc
from stackweave.sync import WaitGroup
got = []
def main():
    port, listeners = rc.serve("127.0.0.1", 0, None, 2)   # all-C accept/echo
    wg = WaitGroup(); wg.add(8)
    def client(i):
        try:
            c = rc.TCPConn.connect("127.0.0.1", port)
            c.send_all(bytes([i]) * 8); got.append(c.recv(8) == bytes([i]) * 8)
            c.close()
        finally:
            wg.done()
    for i in range(8):
        stackweave.fiber(client, i)
    wg.wait()
    for ln in listeners:
        ln.close()
s0 = rc.stats()
stackweave.run(4, main)
s1 = rc.stats()
print("CONLY echoed=%d counted=%d" % (sum(got), sum(
    s1[k] - s0[k] for k in ("fiber_chunks_reused", "fiber_chunks_mapped"))))
'''


def test_c_only_fibers_take_no_chunk():
    # A fiber with no Python callable never pushes a frame, so it must not
    # hold a chunk (an all-C echo server keeps thousands of them parked).
    p = run_python(_C_ONLY, timeout=120)
    assert p.returncode == 0, (p.stdout[-400:], p.stderr[-1600:])
    line = [l for l in p.stdout.splitlines() if l.startswith("CONLY ")]
    assert line, (p.stdout[-400:], p.stderr[-800:])
    # main + the 8 clients are Python fibers; the server's accept and echo
    # fibers are C-only and are not counted.
    assert line[0] == "CONLY echoed=8 counted=9", line[0]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
