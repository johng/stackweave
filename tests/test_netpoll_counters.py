"""The netpoll DIAG counters (STACKWEAVE_NETPOLL_COUNTERS=1).

Off by default, and then stats() has no netpoll_c_* keys.  On, stats() sums
each counter over the per-hub slots.  The slots are allocated when the counters
turn on, not reserved in the module's static data: 16 KB of aligned .bss moved
every later global of the module and cost ping-pong ~7% with the counters off
(netpoll_parkers.c.inc).
"""
import sys

import pytest

from adv_util import run_python

CODE = r"""
import socket
import stackweave, stackweave_c
READ = 1
a, b = socket.socketpair()
a.setblocking(False); b.setblocking(False)
got = []
def reader():
    for _ in range(50):
        assert stackweave_c.wait_fd(a.fileno(), READ, 5000) & READ
        got.append(a.recv(1))
def writer():
    for _ in range(50):
        b.send(b"x")
        stackweave.sleep(0.001)
def main():
    stackweave.fiber(reader)
    stackweave.fiber(writer)
stackweave.run(2, main)
assert len(got) == 50, got
st = stackweave_c.stats()
c = {k[len("netpoll_c_"):]: v for k, v in st.items() if k.startswith("netpoll_c_")}
print("COUNTERS", sorted(c.items()))
print("PASS")
"""


def _counters(env):
    p = run_python(CODE, env=env, timeout=60)
    assert p.returncode == 0 and "PASS" in p.stdout, (
        p.returncode, p.stdout[-2000:], p.stderr[-4000:])
    line = next(l for l in p.stdout.splitlines() if l.startswith("COUNTERS"))
    return dict(eval(line[len("COUNTERS "):]))


def test_counters_are_off_by_default():
    assert _counters({"STACKWEAVE_NETPOLL_COUNTERS": None}) == {}


@pytest.mark.parametrize("value", ["0", ""])
def test_counters_stay_off_for_zero_or_empty(value):
    assert _counters({"STACKWEAVE_NETPOLL_COUNTERS": value}) == {}


def test_counters_count_when_on():
    c = _counters({"STACKWEAVE_NETPOLL_COUNTERS": "1"})
    assert {"pump", "dispatch", "reg_ctl", "backstop_poll"} <= set(c), c
    # the reader's wakes came through the poller's dispatch (both backends;
    # the pump counters are epoll's)
    assert c["dispatch"] > 0, c
    if sys.platform.startswith("linux"):
        assert c["pump"] > 0, c


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
