import socket, time
import stackweave

def main():
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(4)
    port = srv.getsockname()[1]
    def boom(*a, **k): raise RuntimeError("patched getaddrinfo called")
    socket.getaddrinfo = boom

    done = []
    def f():
        c = socket.socket()
        c.connect(("localhost", port))   # hostname -> forces resolution
        print("connect succeeded WITHOUT patched getaddrinfo -> inline libc DNS on hub", flush=True)
        c.close()
        done.append(1)
    stackweave.fiber(f)
    conn, _ = srv.accept()   # keeps srv alive and proves the connect landed
    while not done:
        stackweave.sleep(0.01)
    conn.close(); srv.close()

stackweave.monkey.patch()
stackweave.run(2, main)
