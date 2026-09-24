# repro_migration2.py -- run: timeout 40 .venv/bin/python repro_migration2.py
import socket, sys, time, threading
import stackweave
import stackweave_c as rc
READ, WRITE = 1, 2
res = {}
def main():
    a, b = socket.socketpair()
    a.setblocking(False); b.setblocking(False)
    def reader():
        res["r_tid"] = threading.get_ident()
        t0 = time.monotonic()
        res["r"] = rc.wait_fd(a.fileno(), READ, 8000)
        res["rt"] = time.monotonic() - t0
    stackweave.fiber(reader)
    stackweave.sleep(0.3)
    def writer(i):
        res["w%d_tid" % i] = threading.get_ident()
        res["w%d" % i] = rc.wait_fd(a.fileno(), WRITE, 2000)
    for i in range(8):
        stackweave.fiber(writer, i)
    stackweave.sleep(0.5)
    b.send(b"x")
    stackweave.sleep(1.5)
    res["socks"] = (a, b)
stackweave.run(4, main)
ok = res.get("r") == READ and res.get("rt", 99) < 1.2
print("reader:", res.get("r"), "elapsed %.3fs" % res.get("rt", -1), "OK" if ok else "BUG")
sys.exit(0 if ok else 1)
