# Same as p02 'diff' but two plain OS threads, no stackweave.
import socket, threading, time, sys
a, b = socket.socketpair()
f = a.makefile("rb")
res = []
def reader(i):
    res.append((i, f.readline()))
t0 = threading.Thread(target=reader, args=(0,), daemon=True); t0.start()
time.sleep(0.05)
t1 = threading.Thread(target=reader, args=(1,), daemon=True); t1.start()
time.sleep(0.05)
b.sendall(b"one\ntwo\n")
t0.join(3); t1.join(3)
print(sys.version.split()[0], "GIL" if sys._is_gil_enabled() else "nogil", res, "PASS" if len(res) == 2 else "FAIL: a reader lost buffered data")
