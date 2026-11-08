# Hub 1's OS thread is blocked in C (real time.sleep, tstate detached).  A fiber
# pinned to hub 0 parks in a cooperative recv; main (pinned hub 0) sends.  Does the
# recv wake while hub 1 is blocked?
from prelude import *
import time as _t
real_sleep = _t.sleep
import stackweave.monkey as monkey
monkey.patch()
import socket
H = int(sys.argv[1]) if len(sys.argv) > 1 else 2
BLOCK = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
_watchdog(30)
def main():
    stackweave_c.current_g().pin(0); stackweave.sleep(0)
    a, b = socket.socketpair()
    out = {}
    wg = stackweave.WaitGroup(); wg.add(2)
    def blocker():
        stackweave_c.current_g().pin(1); stackweave.sleep(0)
        out["blocker_hub"] = stackweave_c.mn_current_hub()
        real_sleep(BLOCK)
        wg.done()
    def reader():
        stackweave_c.current_g().pin(0); stackweave.sleep(0)
        out["reader_hub"] = stackweave_c.mn_current_hub()
        t0 = _t.monotonic()
        a.recv(10)
        out["reader_waited"] = _t.monotonic() - t0
        wg.done()
    stackweave.fiber(reader)
    stackweave.sleep(0.05)
    stackweave.fiber(blocker)
    stackweave.sleep(0.2)
    t0 = _t.monotonic()
    b.sendall(b"x")
    out["main_hub"] = stackweave_c.mn_current_hub()
    wg.wait()
    print(out, flush=True)
    print("PASS" if out["reader_waited"] < BLOCK * 0.5 else "FAIL: recv waited for the blocked hub", flush=True)
stackweave.run(H, main)
