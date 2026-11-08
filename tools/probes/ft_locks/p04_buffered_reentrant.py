# Fibers sharing one buffered stream: a fiber parks or is preempted inside the
# raw stream's Python-level read/write while _io.Buffered* holds its lock (owner
# = OS thread id).  Another fiber on the SAME hub then gets
# "RuntimeError: reentrant call inside <_io.Buffered...>" where a thread waits.
#   mode park-read  : socket.makefile('rb'), read(4) parks in recv (monkey sockets)
#   mode preempt-read: Python RawIOBase whose readinto spins (preempted, same hub)
#   mode preempt-write: Python RawIOBase whose write spins
from prelude import *
import io, time as _t
mode = sys.argv[1]
H = int(sys.argv[2]) if len(sys.argv) > 2 else 2
where = sys.argv[3] if len(sys.argv) > 3 else "same"
if mode == "park-read":
    import stackweave.monkey as monkey; monkey.patch()
import socket
_watchdog(20)

def _step(end):
    return _t.monotonic() < end

class SpinRaw(io.RawIOBase):
    def readable(self): return True
    def writable(self): return True
    def _spin(self):
        end = _t.monotonic() + 0.2
        x = 0
        while _step(end):
            x += 1
    def readinto(self, b):
        self._spin(); b[:4] = b"data"; return 4
    def write(self, b):
        self._spin(); return len(b)

def main():
    res = []
    if mode == "park-read":
        a, b = socket.socketpair()
        f = a.makefile("rb")
        op = lambda: f.read(4)
    elif mode == "preempt-read":
        f = io.BufferedReader(SpinRaw(), buffer_size=4)
        op = lambda: f.read(4)
    else:
        f = io.BufferedWriter(SpinRaw(), buffer_size=4)
        op = lambda: (f.write(b"xxxxxxxx"), f.flush())
    wg = stackweave.WaitGroup(); wg.add(2)
    def worker(i):
        try:
            r = op()
            res.append(("ok", i, stackweave_c.mn_current_hub()))
        except Exception as e:
            res.append(("err", i, stackweave_c.mn_current_hub(), repr(e)))
        finally:
            wg.done()
    stackweave_c.mn_fiber(lambda: worker(0), hub=0)
    stackweave.sleep(0.03)
    stackweave_c.mn_fiber(lambda: worker(1), hub=0 if where == "same" else 1)
    stackweave.sleep(0.03)
    if mode == "park-read":
        b.sendall(b"one\ntwo\n")
    wg.wait()
    for r in res: print(r, flush=True)
    st = stackweave_c.stats(); print({k: v for k, v in st.items() if "preempt" in k and v}, flush=True)
    print("PASS" if all(r[0] == "ok" for r in res) else "FAIL", flush=True)
stackweave.run(H, main)
