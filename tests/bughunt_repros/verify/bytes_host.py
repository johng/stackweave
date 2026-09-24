import socket, sys
import stackweave

def battery(tag):
    for h in (b"127.0.0.1", b"localhost"):
        try:
            print(tag, h, socket.getaddrinfo(h, 80, socket.AF_INET, socket.SOCK_STREAM)[0][4], flush=True)
        except Exception as e:
            print(tag, h, type(e).__name__, e, flush=True)

if sys.argv[1] == "stock": battery("stock:")
else:
    def main(): stackweave.fiber(lambda: battery("patched:"))
    stackweave.monkey.patch(); stackweave.run(2, main)
