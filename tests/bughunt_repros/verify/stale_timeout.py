import socket, sys, time
import stackweave

def scenario(tag):
    a, b = socket.socketpair()
    b.settimeout(5.0)
    a.sendall(b"x"); b.recv(1)
    b.settimeout(0)
    t0 = time.monotonic()
    try:
        b.recv(10)
    except BlockingIOError:
        print(tag, "BlockingIOError after %.2fs (correct)" % (time.monotonic()-t0), flush=True)
    except socket.timeout:
        print(tag, "socket.timeout after %.2fs (WRONG)" % (time.monotonic()-t0), flush=True)

if sys.argv[1] == "stock": scenario("stock:")
else:
    def main(): stackweave.fiber(lambda: scenario("patched:"))
    stackweave.monkey.patch(); stackweave.run(2, main)
