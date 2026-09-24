"""Sanity: single-fiber TCPConn recv works in iouring multishot mode."""
import os
os.environ["STACKWEAVE_TCPCONN_IOURING"] = "1"
import socket
import stackweave_c

stackweave_c.mn_init(4)

out = []

def main():
    lst = socket.socket()
    lst.bind(("127.0.0.1", 0)); lst.listen(1)
    cli = socket.socket(); cli.connect(lst.getsockname())
    srv, _ = lst.accept(); lst.close()
    fd = os.dup(srv.fileno()); srv.close()
    conn = stackweave_c.TCPConn(fd)
    cli.sendall(b"hello")
    data = conn.recv(5)
    out.append(data)
    cli.close()
    tail = conn.recv(5)
    out.append(tail)
    conn.close()
    print("got:", out, flush=True)
    os._exit(0)

stackweave_c.mn_fiber(main)
stackweave_c.mn_run()
