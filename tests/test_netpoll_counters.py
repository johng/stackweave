"""The netpoll DIAG counters (STACKWEAVE_NETPOLL_COUNTERS=1).

Off by default, and then stats() has no netpoll_c_* keys.  On, stats() sums
each counter over the per-hub slots.  The slots are allocated when the counters
turn on, not reserved in the module's static data: 16 KB of aligned .bss moved
every later global of the module and cost ping-pong ~7% with the counters off
(netpoll_parkers.c.inc).
"""
import ast
import re
import shutil
import subprocess
import sys

import pytest

import stackweave_c
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
        stackweave.sleep(0.005)          # the reader is parked on the fd by now
        b.send(b"x")
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
    return dict(ast.literal_eval(line[len("COUNTERS "):]))


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



def _static_size(so, name):
    """Size in bytes of the static object `name` in the built extension: ELF
    records it (nm -S); Mach-O does not (its nm prints 0), so there it is the
    gap to the next symbol in address order.  None if nm cannot tell."""
    names = (name, "_" + name)
    out = subprocess.run(["nm", "-S", "--defined-only", so], capture_output=True, text=True)
    if out.returncode == 0:
        for line in out.stdout.splitlines():
            p = line.split()
            if len(p) == 4 and p[3] in names and int(p[1], 16) > 0:
                return int(p[1], 16)
    out = subprocess.run(["nm", "-n", so], capture_output=True, text=True)
    syms = []
    for line in out.stdout.splitlines():
        m = re.match(r"([0-9a-f]+) \w (\S+)$", line)
        if m:
            syms.append((int(m.group(1), 16), m.group(2)))
    for i, (addr, sym) in enumerate(syms):
        if sym in names and i + 1 < len(syms):
            return syms[i + 1][0] - addr
    return None


@pytest.mark.skipif(shutil.which("nm") is None, reason="needs nm to read the symbol table")
def test_counter_slots_are_not_static_data():
    # 16 KB of slots in .bss moved every later global of the module and cost
    # ping-pong ~7% with the counters off; the module now holds only a pointer.
    size = _static_size(stackweave_c.__file__, "runloom_npc")
    assert size is not None, "runloom_npc not found in %s" % stackweave_c.__file__
    assert size <= 64, "runloom_npc takes %d bytes of static data" % size


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
