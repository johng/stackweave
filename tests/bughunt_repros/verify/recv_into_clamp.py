"""Verify: recv_into(buf, n) with n > len(buf).
1. stdlib socket.recv_into -> ValueError (baseline).
2. stackweave_c.TCPConn.recv_into -> ? (claim: silently clamps).
3. monkey-patched socket.socket.recv_into inside a fiber -> ? (tcp_recv fast path).
Also negative n on TCPConn.
"""
import socket
import stackweave_c

# --- 1. stdlib baseline (unpatched, plain thread) ---
a, b = socket.socketpair()
b.sendall(b"x" * 10)
buf = bytearray(4)
try:
    a.recv_into(buf, 16)
    print("STDLIB: no exception (unexpected)")
except ValueError as e:
    print("STDLIB: ValueError:", e)
a.close(); b.close()

# --- 2. TCPConn ---
results = {}

def _drive(*fibers):
    box = []
    def wrap(fn):
        def runner():
            try:
                fn()
            except BaseException as e:
                box.append(e)
        return runner
    for g in fibers:
        stackweave_c.fiber(wrap(g))
    stackweave_c.run()
    if box:
        raise box[0]

def _port(listener):
    s = socket.socket(fileno=socket.dup(listener.fileno()))
    try:
        return s.getsockname()[1]
    finally:
        s.detach(); s.close()

port = [None]

def server():
    ln = stackweave_c.TCPConn.listen("127.0.0.1", 0)
    port[0] = _port(ln)
    conn = ln.accept()
    conn.send_all(b"abcdefghij")   # 10 bytes
    conn.close(); ln.close()

def client():
    while port[0] is None:
        stackweave_c.sched_yield()
    c = stackweave_c.TCPConn.connect("127.0.0.1", port[0])
    buf = bytearray(4)
    try:
        n = c.recv_into(buf, 4096)   # n > len(buf): stdlib would raise
        results["tcpconn_big_n"] = ("returned", n, bytes(buf[:n]))
    except ValueError as e:
        results["tcpconn_big_n"] = ("ValueError", str(e))
    buf2 = bytearray(4)
    try:
        n = c.recv_into(buf2, -5)    # negative: stdlib raises ValueError
        results["tcpconn_neg_n"] = ("returned", n)
    except ValueError as e:
        results["tcpconn_neg_n"] = ("ValueError", str(e))
    c.close()

_drive(server, client)
print("TCPConn recv_into(buf4, 4096):", results["tcpconn_big_n"])
print("TCPConn recv_into(buf4, -5):  ", results["tcpconn_neg_n"])

# --- 3. monkey-patched socket.socket.recv_into inside a fiber ---
import stackweave
stackweave.monkey.patch()

mres = {}

def sock_fiber():
    x, y = socket.socketpair()
    y.sendall(b"0123456789")
    buf = bytearray(4)
    try:
        n = x.recv_into(buf, 4096)
        mres["patched"] = ("returned", n, bytes(buf[:n]))
    except ValueError as e:
        mres["patched"] = ("ValueError", str(e))
    x.close(); y.close()

_drive(sock_fiber)
print("monkey-patched socket.recv_into(buf4, 4096) in fiber:", mres["patched"])

# stdlib comparison for negative n
a, b = socket.socketpair()
stackweave.monkey.unpatch() if hasattr(stackweave.monkey, "unpatch") else None
buf = bytearray(4)
try:
    a.recv_into(buf, -5)
    print("STDLIB neg: no exception (unexpected)")
except ValueError as e:
    print("STDLIB neg: ValueError:", e)
a.close(); b.close()
