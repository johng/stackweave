import socket, sys
import stackweave

def scenario(tag):
    a, b = socket.socketpair()
    b.setblocking(False)
    try:
        print(tag, "recv ->", b.recv(10), flush=True)
    except BlockingIOError:
        print(tag, "BlockingIOError (correct)", flush=True)

if sys.argv[1] == "stock": scenario("stock:")
else:
    def main(): stackweave.fiber(lambda: scenario("patched:"))
    stackweave.monkey.patch(); stackweave.run(2, main)
