"""In-process TCP echo round-trips on the M:N runtime.

The server and the clients are both stackweave fibers on the same hub pool, so
this measures the whole runtime's I/O path (netpoll or the io_uring loop, park
and wake, migration) rather than one side against an external load generator
-- suite/ does that, with a Go loadgen across two netns.  What it is for is
comparing I/O backends and feature switches on one box: the numbers move
when the runtime's I/O path moves.

Two servers, both on loopback, both driven by `CONNS` long-lived TCPConn
clients doing `ROUNDS` 64-byte request/response round-trips each per sample:

  c-echo    stackweave_c.serve(host, 0, None): the built-in all-C echo
            handler.  Under STACKWEAVE_IOURING_LOOP=1 (+ STACKWEAVE_IOURING_MS=1)
            this is the path that uses the hub rings and multishot recv.
  py-echo   serve() with a Python handler: TCPConn.recv + send_all.

Clients are TCPConn, so STACKWEAVE_TCPCONN_IOURING=1 applies to them.

Feature switches are read from the environment when the hubs start, so this
suite measures whatever it is started under; bench.features runs it once per
feature config.  Every sample checks the echoed bytes and the round-trip count.

Run:
    PYTHONPATH=src:benchmark PYTHON_GIL=0 python -m bench.echo
    ... --hubs 2,4,8 --quick --out results/x.json
"""
import argparse
import os
import socket
import struct
import time
import traceback

import stackweave
import stackweave_c

from bench.gil import ensure_nogil
from bench.harness import Suite, default_pin_set

HOST = "127.0.0.1"
MSG = 64


def py_echo_handler(conn):
    while True:
        data = conn.recv(4096)
        if not data:
            break
        conn.send_all(data)


def make_round(conns, rounds):
    """One sample: every client does `rounds` round-trips; a root-fiber
    WaitGroup joins them.  inner = len(conns) * rounds."""
    payload = bytes(range(MSG))

    def once():
        wg = stackweave.WaitGroup()
        wg.add(len(conns))
        bad = []

        def client(conn):
            def f():
                # No memoryview slice is held across the park in recv_into.
                # On an interpreter built from an older copy of src/patches/,
                # a migrating fiber could find such a view's buffer released:
                # memoryview's export counts were a plain ++/-- that a
                # cross-hub dealloc raced (fixed in the exec-home patch,
                # guarded by test_cross_hub_migration's
                # test_memory_memoryview_slice_survives_a_migration).  Kept
                # so the bench also runs there.  A partial read is finished
                # with recv() into a fresh bytes object instead.
                buf = bytearray(MSG)
                try:
                    for _ in range(rounds):
                        conn.send_all(payload)
                        got = conn.recv_into(buf)
                        while 0 < got < MSG:
                            more = conn.recv(MSG - got)
                            if not more:
                                break
                            buf[got:got + len(more)] = more
                            got += len(more)
                        if got <= 0:
                            raise ConnectionError("EOF after %d bytes" % max(got, 0))
                        if buf != payload:
                            raise AssertionError("echo mismatch")
                except Exception:          # noqa: BLE001 -- reported below
                    bad.append(traceback.format_exc())
                finally:
                    wg.done()
            return f

        for c in conns:
            stackweave.fiber(client(c))
        wg.wait()
        if bad:
            raise RuntimeError("%d echo clients failed; first:\n%s"
                               % (len(bad), bad[0]))

    return once


def run_hub_count(s, hubs, conns_n, rounds, servers):
    """Start a hub pool, bring up the servers + clients inside a root fiber,
    run the samples there, and tear everything down so run() returns.  Every
    conn is closed on the way out even on failure: one left open parks its
    server-side handler forever and run() never returns."""
    errors = []

    def root():
        try:
            serve_all()
        except BaseException as e:      # noqa: BLE001 -- re-raised after run()
            errors.append(e)

    def serve_all():
        for label in servers:
            handler = None if label == "c-echo" else py_echo_handler
            port, listeners = stackweave_c.serve(HOST, 0, handler, min(hubs, 8), 1024)
            conns = []
            try:
                for _ in range(conns_n):
                    c = stackweave_c.TCPConn.connect(HOST, port)
                    conns.append(c)
                    c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY,
                                 struct.pack("i", 1))
                s.bench("%s @%dh" % (label, hubs), make_round(conns, rounds),
                        inner=conns_n * rounds,
                        note="%d conns x %d x %dB round-trips, %d hubs"
                             % (conns_n, rounds, MSG, hubs))
            finally:
                for c in conns:
                    c.close()
                for L in listeners:
                    L.close()

    stackweave.run(hubs, root)
    if errors:
        raise errors[0]


def main(argv=None):
    ensure_nogil()
    ap = argparse.ArgumentParser(description="in-process TCP echo round-trips")
    ap.add_argument("--hubs", default="2,4,8",
                    help="comma-separated hub counts (default 2,4,8)")
    ap.add_argument("--conns", type=int, default=64)
    ap.add_argument("--rounds", type=int, default=500,
                    help="round-trips per connection per sample")
    ap.add_argument("--servers", default="c-echo,py-echo")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    hubs = [int(h) for h in args.hubs.split(",") if h]
    hubs = [h for h in hubs if h <= (os.cpu_count() or h)]
    rounds = max(10, args.rounds // (10 if args.quick else 1))
    servers = [x for x in args.servers.split(",") if x]
    s = Suite("echo", pin_cpus=default_pin_set(n=max(hubs) * 2),
              samples=3 if args.quick else 10, warmup=1 if args.quick else 2)
    s.banner()
    loop = os.environ.get("STACKWEAVE_IOURING_LOOP", "0") not in ("", "0")
    print("io path: %s%s%s\n" % (
        "io_uring loop" if loop and stackweave_c.iouring_available()
        else stackweave_c.netpoll_backend(),
        " + multishot" if loop and os.environ.get("STACKWEAVE_IOURING_MS", "0") != "0" else "",
        " + TCPConn io_uring" if os.environ.get("STACKWEAVE_TCPCONN_IOURING", "0") != "0" else ""))
    t0 = time.perf_counter()
    for h in hubs:
        run_hub_count(s, h, args.conns, rounds, servers)
    if loop and stackweave_c.iouring_available():
        st = stackweave_c.stats()
        print("  io_uring loop ran: waits=%s polls=%s"
              % (st.get("iouring_loop_waits"), st.get("iouring_loop_polls")))
    print("  (%.1fs)" % (time.perf_counter() - t0))
    s.write(args.out)


if __name__ == "__main__":
    main()
