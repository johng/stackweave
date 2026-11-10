"""A hub that stops polling must not hold up the fds its poller holds.

An fd is polled by one hub: on epoll the hub whose epoll it was first added to,
on kqueue the hub its waiter parked on.  When that hub cannot poll -- it is
running one long fiber, or it is stuck in a C call -- sysmon polls for it once
the hub has gone the backstop budget without starting a poll
(STACKWEAVE_NETPOLL_BACKSTOP_MS, default 5 ms), so the waiter wakes and runs on
another hub instead of waiting for the stuck one.  Go's sysmon does the same
for its poller (10 ms).

The test parks a reader on hub 0, wedges hub 0's thread in a C sleep, writes
from a plain OS thread, and requires the reader to wake on another hub while
hub 0 is still wedged.  The writer is not a fiber: one that happened to sit on
hub 0 would wait out the wedge too (its sleep timer is on hub 0's heap), which
is a different gap from the one under test.
"""
import ctypes
import ctypes.util
import socket
import threading
import time

import stackweave
import stackweave_c

READ = 1
HUBS = 4
WEDGE_S = 1.5
# The backstop answers in about budget + one sysmon tick (5 + 10 ms); this
# leaves room for a loaded CI runner and is still far below the wedge.
WAKE_LIMIT_S = 0.5


def _move_to_hub(hub):
    """Resume the calling fiber on `hub`: pin, then let a timer wake it there
    (a pinned fiber's timer wakes it through the global run-queue, which
    honours the pin)."""
    g = stackweave_c.current_g()
    g.pin(hub)
    for _ in range(200):
        if stackweave_c.mn_current_hub() == hub:
            return g
        stackweave.sleep(0.001)
    raise AssertionError("never resumed on hub %d" % hub)


def test_reader_on_a_wedged_hub_wakes_on_another_hub():
    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    st = {}

    def reader():
        g = _move_to_hub(0)
        g.pin(None)                       # still on hub 0; its wake may go anywhere
        st["park_hub"] = stackweave_c.mn_current_hub()
        st["parked"] = True
        st["ready"] = stackweave_c.wait_fd(a.fileno(), READ, 10000)
        st["woke_at"] = time.monotonic()
        st["wake_hub"] = stackweave_c.mn_current_hub()

    def wedge():
        _move_to_hub(0)
        st["wedged_at"] = time.monotonic()
        libc.usleep(int(WEDGE_S * 1e6))   # hub 0's thread: no poll there meanwhile
        st["unwedged_at"] = time.monotonic()
        stackweave_c.current_g().pin(None)

    def writer():                         # a plain OS thread, not a fiber
        deadline = time.monotonic() + 30
        while "wedged_at" not in st and time.monotonic() < deadline:
            time.sleep(0.001)
        time.sleep(0.05)
        st["sent_at"] = time.monotonic()
        b.send(b"x")

    def main():                           # run() returns once both fibers finish
        stackweave.fiber(reader)
        while not st.get("parked"):
            stackweave.sleep(0.001)
        stackweave.sleep(0.02)            # let it reach the park
        stackweave.fiber(wedge)
        t = threading.Thread(target=writer, daemon=True)
        t.start()
        st["writer"] = t

    try:
        stackweave.run(HUBS, main)
        st["writer"].join(30)
    finally:
        a.close()
        b.close()

    assert st["park_hub"] == 0
    assert st["ready"] & READ, st
    late = st["woke_at"] - st["sent_at"]
    assert late < WAKE_LIMIT_S, (
        "the reader waited %.3f s for its wedged hub (wedge %.1f s): nothing "
        "polled hub 0's fds while it was stuck" % (late, WEDGE_S))
    assert st["woke_at"] < st["unwedged_at"], st
    assert st["wake_hub"] != 0, st
