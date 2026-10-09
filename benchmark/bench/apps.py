"""Application-shaped workloads on stackweave (see bench/appspec.py).

An HTTP/1.1 JSON API, an API gateway fanning out to slow backends, a
CPU-bound parse+hash pipeline, a pub/sub broadcast and a 10k-task crawler,
all on one M:N hub pool (STACKWEAVE_BENCH_HUBS, default 4) inside one
stackweave.run, timed from a root fiber.  bench.baselines and gobench/ run
the same programs; bench.compare puts them in one table.  Every sample checks
its result (decoded ids, checksums, counts).

Run:
    PYTHONPATH=src:benchmark PYTHON_GIL=0 python -m bench.apps [--quick] [--out x.json]
"""
import argparse
import os
import socket
import struct

import stackweave
import stackweave_c

from bench import appspec as A
from bench.gil import ensure_nogil
from bench.harness import Suite, default_pin_set

HOST = "127.0.0.1"
HUBS = int(os.environ.get("STACKWEAVE_BENCH_HUBS", "4"))
Chan = stackweave_c.Chan


def _join(fns):
    wg = stackweave.WaitGroup()
    wg.add(len(fns))

    def wrap(f):
        def g():
            try:
                f()
            finally:
                wg.done()
        return g
    for f in fns:
        stackweave.fiber(wrap(f))
    wg.wait()


def http_bench(s, n_reqs):
    def handler(conn):
        fr = A.Framer(conn.recv)
        while True:
            head = fr.head()
            if head is None:
                return
            conn.send_all(A.http_handle(head))

    port, listeners = stackweave_c.serve(HOST, 0, handler, min(HUBS, 8), 1024)
    conns = []
    try:
        for _ in range(A.HTTP_CONNS):
            c = stackweave_c.TCPConn.connect(HOST, port)
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, struct.pack("i", 1))
            conns.append(c)

        def client(ci, c):
            def f():
                fr = A.Framer(c.recv)
                for i in range(n_reqs):
                    uid = A.uid_of(ci, i)
                    c.send_all(A.http_request(uid))
                    head = fr.head()
                    A.http_check(head, fr.body(A.content_length(head)), uid)
            return f

        s.bench(A.ROWS["http"], lambda: _join([client(i, c) for i, c in enumerate(conns)]),
                inner=A.HTTP_CONNS * n_reqs, note="stackweave_c.serve + TCPConn clients")
    finally:
        for c in conns:
            c.close()
        for L in listeners:
            L.close()


def gateway_once(n):
    res = [0] * (n * A.GW_FANOUT)

    def request(r):
        def call(k):
            def f():
                stackweave.sleep(A.GW_LATENCY_S)
                res[r * A.GW_FANOUT + k] = A.backend_work(r, k)
            return f
        _join([call(k) for k in range(A.GW_FANOUT)])

    def worker(w):
        def f():
            for r in range(w, n, A.GW_INFLIGHT):
                request(r)
        return f
    _join([worker(w) for w in range(A.GW_INFLIGHT)])
    if sum(res) != A.gateway_expected(n):
        raise AssertionError("gateway checksum")


_DONE = object()


def pipeline_once(n):
    recs = A.pipe_records(n)
    work, out = Chan(A.PIPE_CAP), Chan(A.PIPE_CAP)

    def producer():
        for r in recs:
            work.send(r)
        work.close()

    def worker():
        while True:
            r, ok = work.recv()
            if not ok:
                break
            out.send(A.pipe_work(r))
        out.send(_DONE)

    stackweave.fiber(producer)
    for _ in range(A.PIPE_WORKERS):
        stackweave.fiber(worker)
    total, done = 0, 0
    while done < A.PIPE_WORKERS:
        v, _ = out.recv()
        if v is _DONE:
            done += 1
        else:
            total += v
    if total != A.pipe_expected(n):
        raise AssertionError("pipeline checksum")


def pubsub_once(msgs):
    subs = [Chan(A.PUB_CAP) for _ in range(A.PUB_SUBS)]
    got = [0] * A.PUB_SUBS

    def sub(k):
        def f():
            ch, acc = subs[k], 0
            while True:
                v, ok = ch.recv()
                if not ok:
                    break
                acc += v
            got[k] = acc
        return f

    def publisher():
        for i in range(msgs):
            for ch in subs:
                ch.send(i)
        for ch in subs:
            ch.close()
    _join([sub(k) for k in range(A.PUB_SUBS)] + [publisher])
    if any(g != msgs * (msgs - 1) // 2 for g in got):
        raise AssertionError("pub/sub lost a message")


def crawl_once(n):
    acc = [0] * n

    def task(t):
        def f():
            a = 0
            for k in range(A.CRAWL_FETCHES):
                stackweave.sleep(A.crawl_latency_s(t, k))
                a += A.crawl_parse(t, k)
            acc[t] = a
        return f
    _join([task(t) for t in range(n)])
    if sum(acc) != A.crawl_expected(n):
        raise AssertionError("crawl checksum")


def main(argv=None):
    ensure_nogil()
    ap = argparse.ArgumentParser(description="application-shaped workloads on stackweave")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    z = A.scaled(args.quick)
    s = Suite("apps", pin_cpus=default_pin_set(n=HUBS * 2),
              samples=3 if args.quick else 10, warmup=1 if args.quick else 2)
    s.banner()
    A.pipe_expected(z["pipe_records"])          # build the records untimed
    errors = []

    def root():
        try:
            http_bench(s, z["http_reqs"])
            n = z["gw_requests"]
            s.bench(A.ROWS["gateway"], lambda: gateway_once(n), inner=n,
                    note="fiber per backend call + WaitGroup")
            n2 = z["pipe_records"]
            s.bench(A.ROWS["pipeline"], lambda: pipeline_once(n2), inner=n2,
                    note="Chan(256) -> 4 worker fibers -> Chan(256)")
            m = z["pub_msgs"]
            s.bench(A.ROWS["pubsub"], lambda: pubsub_once(m), inner=m * A.PUB_SUBS,
                    note="one Chan(16) per subscriber")
            n3 = z["crawl_tasks"]
            s.bench(A.ROWS["crawl"], lambda: crawl_once(n3), inner=n3 * A.CRAWL_FETCHES,
                    note="stackweave.sleep per fetch")
        except BaseException as e:          # noqa: BLE001 -- re-raised after run()
            errors.append(e)

    stackweave.run(HUBS, root)
    if errors:
        raise errors[0]
    s.write(args.out)


if __name__ == "__main__":
    main()
