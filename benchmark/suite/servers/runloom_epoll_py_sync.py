"""Std name: runloom_epoll_py_sync  (this file ALSO backs runloom_iouring_py_sync,
launched byte-for-byte with env STACKWEAVE_IOURING_LOOP=1).

Server tier 1 (epoll) / tier 3 (io_uring): stackweave default backend, ZERO
optimized -- the naive, object-heavy path.

Spec: wrapped python calls, no direct C calls, python objects.
    listener = stackweave.sync.tcp_listen(...)
    while True:
        conn, _ = listener.accept()
        stackweave.go(handle, conn)      # real name: stackweave.fiber

The handler uses recv() (allocates a bytes per read) + sendall(bytes) on the
high-level stackweave.sync.Socket facade -- deliberately the slow tier.
Tier 3 is byte-for-byte this file; the orchestrator just exports
STACKWEAVE_IOURING_LOOP=1 (spec: "same code as 1 but io_uring loop").
"""
import argparse
import os

import stackweave
import stackweave.sync as rs


def handle(conn):
    try:
        while True:
            data = conn.recv(65536)          # python bytes alloc per read
            if not data:
                break
            conn.sendall(data)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="10.99.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--hubs", type=int, default=int((os.cpu_count() or 1) * 0.7))
    ap.add_argument("--token", default="")   # for targeted pkill by the orchestrator
    args = ap.parse_args()

    def root():
        ln = rs.tcp_listen(args.host, args.port, backlog=4096)
        port = ln.getsockname()[1]
        print("LISTENING %d" % port, flush=True)
        while True:
            conn, _ = ln.accept()
            stackweave.fiber(handle, conn)

    stackweave.run(args.hubs, root)


if __name__ == "__main__":
    main()
