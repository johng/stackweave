"""bench.apps / bench.memory on threads, asyncio, uvloop, trio and gevent.

The same programs as bench/apps.py and bench/memory.py (defined in
bench/appspec.py), written the way each runtime is normally used: threads
with a thread per connection / task and a ThreadPoolExecutor for the
gateway's backend calls, asyncio streams and gather, trio nurseries and
memory channels, gevent greenlets and gevent.queue.  Driven by
`python -m bench.baselines --kind K --suite apps|memory`.
"""
import gc
import queue
import socket
import threading
import time

from bench import appspec as A

HOST = "127.0.0.1"
_STOP = object()


def _threads(fns):
    ts = [threading.Thread(target=f) for f in fns]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


def _settle_until(ready, n, sleep):
    while sum(ready) < n:
        sleep(0.01)
    sleep(0.2)


# ====================================================================
class ThreadsApps:
    def run(self, s, z):
        self.http(s, z["http_reqs"])
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=A.GW_INFLIGHT * A.GW_FANOUT) as pool:
            n = z["gw_requests"]
            s.bench(A.ROWS["gateway"], lambda: self.gateway(pool, n), inner=n,
                    note="100 request threads, backend calls on a ThreadPoolExecutor(800)")
        n = z["pipe_records"]
        s.bench(A.ROWS["pipeline"], lambda: self.pipeline(n), inner=n,
                note="queue.Queue(256) -> 4 worker threads")
        m = z["pub_msgs"]
        s.bench(A.ROWS["pubsub"], lambda: self.pubsub(m), inner=m * A.PUB_SUBS,
                note="a thread + queue.Queue(16) per subscriber")
        n = z["crawl_tasks"]
        s.bench(A.ROWS["crawl"], lambda: self.crawl(n), inner=n * A.CRAWL_FETCHES,
                note="a thread per task")

    def http(self, s, n_reqs):
        srv = socket.create_server((HOST, 0), backlog=1024)
        port = srv.getsockname()[1]

        def handle(c):
            with c:
                fr = A.Framer(c.recv)
                while True:
                    head = fr.head()
                    if head is None:
                        return
                    c.sendall(A.http_handle(head))

        def acceptor():
            for _ in range(A.HTTP_CONNS):
                c, _ = srv.accept()
                c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                threading.Thread(target=handle, args=(c,), daemon=True).start()
        acc = threading.Thread(target=acceptor)
        acc.start()
        conns = [socket.create_connection((HOST, port)) for _ in range(A.HTTP_CONNS)]
        for c in conns:
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        acc.join()

        def client(ci, c, bad):
            def f():
                try:
                    fr = A.Framer(c.recv)
                    for i in range(n_reqs):
                        uid = A.uid_of(ci, i)
                        c.sendall(A.http_request(uid))
                        head = fr.head()
                        A.http_check(head, fr.body(A.content_length(head)), uid)
                except Exception as e:      # noqa: BLE001 -- reported below
                    bad.append(e)
            return f

        def once():
            bad = []
            _threads([client(i, c, bad) for i, c in enumerate(conns)])
            if bad:
                raise bad[0]
        try:
            s.bench(A.ROWS["http"], once, inner=A.HTTP_CONNS * n_reqs,
                    note="a server thread per conn + 64 client threads")
        finally:
            for c in conns:
                c.close()
            srv.close()

    def gateway(self, pool, n):
        res = [0] * (n * A.GW_FANOUT)

        def call(r, k):
            time.sleep(A.GW_LATENCY_S)
            return A.backend_work(r, k)

        def worker(w):
            def f():
                for r in range(w, n, A.GW_INFLIGHT):
                    futs = [pool.submit(call, r, k) for k in range(A.GW_FANOUT)]
                    for k, fu in enumerate(futs):
                        res[r * A.GW_FANOUT + k] = fu.result()
            return f
        _threads([worker(w) for w in range(A.GW_INFLIGHT)])
        if sum(res) != A.gateway_expected(n):
            raise AssertionError("gateway checksum")

    def pipeline(self, n):
        recs = A.pipe_records(n)
        work, out = queue.Queue(A.PIPE_CAP), queue.Queue(A.PIPE_CAP)

        def producer():
            for r in recs:
                work.put(r)
            for _ in range(A.PIPE_WORKERS):
                work.put(_STOP)

        def worker():
            while True:
                r = work.get()
                if r is _STOP:
                    break
                out.put(A.pipe_work(r))
            out.put(_STOP)
        ts = [threading.Thread(target=producer)] + [
            threading.Thread(target=worker) for _ in range(A.PIPE_WORKERS)]
        for t in ts:
            t.start()
        total, done = 0, 0
        while done < A.PIPE_WORKERS:
            v = out.get()
            if v is _STOP:
                done += 1
            else:
                total += v
        for t in ts:
            t.join()
        if total != A.pipe_expected(n):
            raise AssertionError("pipeline checksum")

    def pubsub(self, msgs):
        qs = [queue.Queue(A.PUB_CAP) for _ in range(A.PUB_SUBS)]
        got = [0] * A.PUB_SUBS

        def sub(k):
            def f():
                q, acc = qs[k], 0
                while True:
                    v = q.get()
                    if v is _STOP:
                        break
                    acc += v
                got[k] = acc
            return f

        def publisher():
            for i in range(msgs):
                for q in qs:
                    q.put(i)
            for q in qs:
                q.put(_STOP)
        _threads([sub(k) for k in range(A.PUB_SUBS)] + [publisher])
        if any(g != msgs * (msgs - 1) // 2 for g in got):
            raise AssertionError("pub/sub lost a message")

    def crawl(self, n):
        acc = [0] * n

        def task(t):
            def f():
                a = 0
                for k in range(A.CRAWL_FETCHES):
                    time.sleep(A.crawl_latency_s(t, k))
                    a += A.crawl_parse(t, k)
                acc[t] = a
            return f
        _threads([task(t) for t in range(n)])
        if sum(acc) != A.crawl_expected(n):
            raise AssertionError("crawl checksum")

    def memory(self, s, counts):
        for n in counts:
            if n > A.MEM_THREADS_MAX:
                continue
            ev = threading.Event()
            ready = bytearray(n)

            def unit(k):
                def f():
                    ready[k] = 1
                    ev.wait()
                return f
            gc.collect()
            before = A.rss_bytes()
            ts = [threading.Thread(target=unit(k)) for k in range(n)]
            for t in ts:
                t.start()
            _settle_until(ready, n, time.sleep)
            after = A.rss_bytes()
            s.memory(A.mem_row(n), n, before, after, note="threads parked on Event.wait")
            ev.set()
            for t in ts:
                t.join()


# ====================================================================
class AsyncioApps:
    """asyncio and uvloop: `loop` is the runtime's persistent loop."""

    def __init__(self, loop):
        import asyncio
        self.aio = asyncio
        self.loop = loop

    def _bench(self, s, name, coro_fn, inner, note):
        s.bench(name, lambda: self.loop.run_until_complete(coro_fn()), inner=inner, note=note)

    def run(self, s, z):
        self.http(s, z["http_reqs"])
        n = z["gw_requests"]
        self._bench(s, A.ROWS["gateway"], lambda: self.gateway(n), n, "gather per request")
        n2 = z["pipe_records"]
        self._bench(s, A.ROWS["pipeline"], lambda: self.pipeline(n2), n2,
                    "asyncio.Queue(256) -> 4 worker tasks (one thread: no parallelism)")
        m = z["pub_msgs"]
        self._bench(s, A.ROWS["pubsub"], lambda: self.pubsub(m), m * A.PUB_SUBS,
                    "a task + asyncio.Queue(16) per subscriber")
        n3 = z["crawl_tasks"]
        self._bench(s, A.ROWS["crawl"], lambda: self.crawl(n3), n3 * A.CRAWL_FETCHES,
                    "a task per crawl")

    def http(self, s, n_reqs):
        aio = self.aio

        async def handle(r, w):
            fr = A.AsyncFramer(r.read)
            try:
                while True:
                    head = await fr.head()
                    if head is None:
                        break
                    w.write(A.http_handle(head))
                    await w.drain()
            finally:
                w.close()

        async def setup():
            srv = await aio.start_server(handle, HOST, 0, backlog=1024)
            port = srv.sockets[0].getsockname()[1]
            conns = []
            for _ in range(A.HTTP_CONNS):
                r, w = await aio.open_connection(HOST, port)
                w.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conns.append((A.AsyncFramer(r.read), w))
            return srv, conns
        srv, conns = self.loop.run_until_complete(setup())

        async def client(ci, fr, w):
            for i in range(n_reqs):
                uid = A.uid_of(ci, i)
                w.write(A.http_request(uid))
                await w.drain()
                head = await fr.head()
                A.http_check(head, await fr.body(A.content_length(head)), uid)

        async def all_clients():
            await aio.gather(*(client(i, fr, w) for i, (fr, w) in enumerate(conns)))

        async def close():
            for _, w in conns:
                w.close()
            srv.close()
            await srv.wait_closed()
        try:
            self._bench(s, A.ROWS["http"], all_clients, A.HTTP_CONNS * n_reqs,
                        "streams server + 64 client tasks, one loop")
        finally:
            self.loop.run_until_complete(close())

    async def gateway(self, n):
        aio = self.aio
        res = [0] * (n * A.GW_FANOUT)

        async def call(r, k):
            await aio.sleep(A.GW_LATENCY_S)
            res[r * A.GW_FANOUT + k] = A.backend_work(r, k)

        async def worker(w):
            for r in range(w, n, A.GW_INFLIGHT):
                await aio.gather(*(call(r, k) for k in range(A.GW_FANOUT)))
        await aio.gather(*(worker(w) for w in range(A.GW_INFLIGHT)))
        if sum(res) != A.gateway_expected(n):
            raise AssertionError("gateway checksum")

    async def pipeline(self, n):
        aio = self.aio
        recs = A.pipe_records(n)
        work, out = aio.Queue(A.PIPE_CAP), aio.Queue(A.PIPE_CAP)

        async def producer():
            for r in recs:
                await work.put(r)
            for _ in range(A.PIPE_WORKERS):
                await work.put(_STOP)

        async def worker():
            while True:
                r = await work.get()
                if r is _STOP:
                    break
                await out.put(A.pipe_work(r))
            await out.put(_STOP)

        async def aggregate():
            total, done = 0, 0
            while done < A.PIPE_WORKERS:
                v = await out.get()
                if v is _STOP:
                    done += 1
                else:
                    total += v
            return total
        rs = await aio.gather(producer(), aggregate(), *(worker() for _ in range(A.PIPE_WORKERS)))
        if rs[1] != A.pipe_expected(n):
            raise AssertionError("pipeline checksum")

    async def pubsub(self, msgs):
        aio = self.aio
        qs = [aio.Queue(A.PUB_CAP) for _ in range(A.PUB_SUBS)]
        got = [0] * A.PUB_SUBS

        async def sub(k):
            q, acc = qs[k], 0
            while True:
                v = await q.get()
                if v is _STOP:
                    break
                acc += v
            got[k] = acc

        async def publisher():
            for i in range(msgs):
                for q in qs:
                    await q.put(i)
            for q in qs:
                await q.put(_STOP)
        await aio.gather(publisher(), *(sub(k) for k in range(A.PUB_SUBS)))
        if any(g != msgs * (msgs - 1) // 2 for g in got):
            raise AssertionError("pub/sub lost a message")

    async def crawl(self, n):
        aio = self.aio
        acc = [0] * n

        async def task(t):
            a = 0
            for k in range(A.CRAWL_FETCHES):
                await aio.sleep(A.crawl_latency_s(t, k))
                a += A.crawl_parse(t, k)
            acc[t] = a
        await aio.gather(*(task(t) for t in range(n)))
        if sum(acc) != A.crawl_expected(n):
            raise AssertionError("crawl checksum")

    def memory(self, s, counts):
        aio = self.aio

        async def park(n):
            ev = aio.Event()
            ready = bytearray(n)

            async def unit(k):
                ready[k] = 1
                await ev.wait()
            gc.collect()
            before = A.rss_bytes()
            ts = [aio.ensure_future(unit(k)) for k in range(n)]
            while sum(ready) < n:
                await aio.sleep(0.01)
            await aio.sleep(0.2)
            after = A.rss_bytes()
            s.memory(A.mem_row(n), n, before, after, note="tasks parked on Event.wait")
            ev.set()
            await aio.gather(*ts)
        for n in counts:
            self.loop.run_until_complete(park(n))


# ====================================================================
class TrioApps:
    def __init__(self):
        import trio
        self.trio = trio

    def _bench(self, s, name, coro_fn, inner, note):
        s.bench(name, lambda: self.trio.run(coro_fn), inner=inner, note=note)

    async def _join(self, fns):
        async with self.trio.open_nursery() as nur:
            for f in fns:
                nur.start_soon(f)

    def run(self, s, z):
        self.http(s, z["http_reqs"], s.samples, s.warmup)
        n = z["gw_requests"]
        self._bench(s, A.ROWS["gateway"], lambda: self.gateway(n), n, "nursery per request")
        n2 = z["pipe_records"]
        self._bench(s, A.ROWS["pipeline"], lambda: self.pipeline(n2), n2,
                    "memory channels(256) -> 4 worker tasks (one thread)")
        m = z["pub_msgs"]
        self._bench(s, A.ROWS["pubsub"], lambda: self.pubsub(m), m * A.PUB_SUBS,
                    "a task + memory channel(16) per subscriber")
        n3 = z["crawl_tasks"]
        self._bench(s, A.ROWS["crawl"], lambda: self.crawl(n3), n3 * A.CRAWL_FETCHES,
                    "a task per crawl")

    def http(self, s, n_reqs, samples, warmup):
        import functools
        trio = self.trio

        async def handle(stream):
            async with stream:
                fr = A.AsyncFramer(stream.receive_some)
                while True:
                    head = await fr.head()
                    if head is None:
                        return
                    await stream.send_all(A.http_handle(head))

        async def client(ci, st, fr):
            for i in range(n_reqs):
                uid = A.uid_of(ci, i)
                await st.send_all(A.http_request(uid))
                head = await fr.head()
                A.http_check(head, await fr.body(A.content_length(head)), uid)

        async def go():
            async with trio.open_nursery() as nur:
                ls = await nur.start(functools.partial(trio.serve_tcp, handle, 0, host=HOST))
                port = ls[0].socket.getsockname()[1]
                conns = []
                for _ in range(A.HTTP_CONNS):
                    st = await trio.open_tcp_stream(HOST, port)
                    st.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    conns.append((st, A.AsyncFramer(st.receive_some)))
                times = []
                for i in range(warmup + samples):
                    t0 = time.perf_counter_ns()
                    async with trio.open_nursery() as cn:
                        for ci, (st, fr) in enumerate(conns):
                            cn.start_soon(client, ci, st, fr)
                    if i >= warmup:
                        times.append((time.perf_counter_ns() - t0) / 1e9)
                for st, _ in conns:
                    await st.aclose()
                nur.cancel_scope.cancel()
            return times
        s.record(A.ROWS["http"], trio.run(go), inner=A.HTTP_CONNS * n_reqs,
                 note="serve_tcp + 64 client tasks, one trio.run")

    async def gateway(self, n):
        trio = self.trio
        res = [0] * (n * A.GW_FANOUT)

        async def call(r, k):
            await trio.sleep(A.GW_LATENCY_S)
            res[r * A.GW_FANOUT + k] = A.backend_work(r, k)

        async def worker(w):
            for r in range(w, n, A.GW_INFLIGHT):
                async with trio.open_nursery() as nur:
                    for k in range(A.GW_FANOUT):
                        nur.start_soon(call, r, k)
        async with trio.open_nursery() as nur:
            for w in range(A.GW_INFLIGHT):
                nur.start_soon(worker, w)
        if sum(res) != A.gateway_expected(n):
            raise AssertionError("gateway checksum")

    async def pipeline(self, n):
        trio = self.trio
        recs = A.pipe_records(n)
        w_s, w_r = trio.open_memory_channel(A.PIPE_CAP)
        o_s, o_r = trio.open_memory_channel(A.PIPE_CAP)
        total = [0]

        async def producer():
            for r in recs:
                await w_s.send(r)
            for _ in range(A.PIPE_WORKERS):
                await w_s.send(_STOP)

        async def worker():
            while True:
                r = await w_r.receive()
                if r is _STOP:
                    break
                await o_s.send(A.pipe_work(r))
            await o_s.send(_STOP)

        async def aggregate():
            done = 0
            while done < A.PIPE_WORKERS:
                v = await o_r.receive()
                if v is _STOP:
                    done += 1
                else:
                    total[0] += v
        await self._join([producer, aggregate] + [worker] * A.PIPE_WORKERS)
        if total[0] != A.pipe_expected(n):
            raise AssertionError("pipeline checksum")

    async def pubsub(self, msgs):
        trio = self.trio
        chans = [trio.open_memory_channel(A.PUB_CAP) for _ in range(A.PUB_SUBS)]
        got = [0] * A.PUB_SUBS

        def sub(k):
            async def f():
                acc = 0
                async for v in chans[k][1]:
                    acc += v
                got[k] = acc
            return f

        async def publisher():
            for i in range(msgs):
                for snd, _ in chans:
                    await snd.send(i)
            for snd, _ in chans:
                await snd.aclose()
        await self._join([sub(k) for k in range(A.PUB_SUBS)] + [publisher])
        if any(g != msgs * (msgs - 1) // 2 for g in got):
            raise AssertionError("pub/sub lost a message")

    async def crawl(self, n):
        trio = self.trio
        acc = [0] * n

        async def task(t):
            a = 0
            for k in range(A.CRAWL_FETCHES):
                await trio.sleep(A.crawl_latency_s(t, k))
                a += A.crawl_parse(t, k)
            acc[t] = a
        async with trio.open_nursery() as nur:
            for t in range(n):
                nur.start_soon(task, t)
        if sum(acc) != A.crawl_expected(n):
            raise AssertionError("crawl checksum")

    def memory(self, s, counts):
        trio = self.trio

        async def park(n):
            ev = trio.Event()
            ready = bytearray(n)

            async def unit(k):
                ready[k] = 1
                await ev.wait()
            gc.collect()
            before = A.rss_bytes()
            async with trio.open_nursery() as nur:
                for k in range(n):
                    nur.start_soon(unit, k)
                while sum(ready) < n:
                    await trio.sleep(0.01)
                await trio.sleep(0.2)
                after = A.rss_bytes()
                s.memory(A.mem_row(n), n, before, after, note="tasks parked on Event.wait")
                ev.set()
        for n in counts:
            trio.run(park, n)


# ====================================================================
class GeventApps:
    def __init__(self):
        import gevent
        import gevent.event
        import gevent.queue
        self.g = gevent

    def _join(self, fns):
        self.g.joinall([self.g.spawn(f) for f in fns], raise_error=True)

    def run(self, s, z):
        self.http(s, z["http_reqs"])
        n = z["gw_requests"]
        s.bench(A.ROWS["gateway"], lambda: self.gateway(n), inner=n,
                note="greenlet per backend call + joinall")
        n2 = z["pipe_records"]
        s.bench(A.ROWS["pipeline"], lambda: self.pipeline(n2), inner=n2,
                note="gevent.queue.Queue(256) -> 4 worker greenlets (one thread)")
        m = z["pub_msgs"]
        s.bench(A.ROWS["pubsub"], lambda: self.pubsub(m), inner=m * A.PUB_SUBS,
                note="a greenlet + Queue(16) per subscriber")
        n3 = z["crawl_tasks"]
        s.bench(A.ROWS["crawl"], lambda: self.crawl(n3), inner=n3 * A.CRAWL_FETCHES,
                note="a greenlet per crawl")

    def http(self, s, n_reqs):
        from gevent import socket as gsocket
        from gevent.server import StreamServer

        def handle(c, _addr):
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            fr = A.Framer(c.recv)
            while True:
                head = fr.head()
                if head is None:
                    return
                c.sendall(A.http_handle(head))
        srv = StreamServer((HOST, 0), handle, backlog=1024)
        srv.start()
        conns = []
        for _ in range(A.HTTP_CONNS):
            c = gsocket.create_connection((HOST, srv.server_port))
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conns.append(c)

        def client(ci, c):
            def f():
                fr = A.Framer(c.recv)
                for i in range(n_reqs):
                    uid = A.uid_of(ci, i)
                    c.sendall(A.http_request(uid))
                    head = fr.head()
                    A.http_check(head, fr.body(A.content_length(head)), uid)
            return f
        try:
            s.bench(A.ROWS["http"], lambda: self._join([client(i, c) for i, c in enumerate(conns)]),
                    inner=A.HTTP_CONNS * n_reqs, note="StreamServer + 64 client greenlets")
        finally:
            for c in conns:
                c.close()
            srv.stop()

    def gateway(self, n):
        g = self.g
        res = [0] * (n * A.GW_FANOUT)

        def call(r, k):
            g.sleep(A.GW_LATENCY_S)
            res[r * A.GW_FANOUT + k] = A.backend_work(r, k)

        def worker(w):
            def f():
                for r in range(w, n, A.GW_INFLIGHT):
                    g.joinall([g.spawn(call, r, k) for k in range(A.GW_FANOUT)], raise_error=True)
            return f
        self._join([worker(w) for w in range(A.GW_INFLIGHT)])
        if sum(res) != A.gateway_expected(n):
            raise AssertionError("gateway checksum")

    def pipeline(self, n):
        g = self.g
        recs = A.pipe_records(n)
        work, out = g.queue.Queue(A.PIPE_CAP), g.queue.Queue(A.PIPE_CAP)
        total = [0]

        def producer():
            for r in recs:
                work.put(r)
            for _ in range(A.PIPE_WORKERS):
                work.put(_STOP)

        def worker():
            while True:
                r = work.get()
                if r is _STOP:
                    break
                out.put(A.pipe_work(r))
            out.put(_STOP)

        def aggregate():
            done = 0
            while done < A.PIPE_WORKERS:
                v = out.get()
                if v is _STOP:
                    done += 1
                else:
                    total[0] += v
        self._join([producer, aggregate] + [worker] * A.PIPE_WORKERS)
        if total[0] != A.pipe_expected(n):
            raise AssertionError("pipeline checksum")

    def pubsub(self, msgs):
        g = self.g
        qs = [g.queue.Queue(A.PUB_CAP) for _ in range(A.PUB_SUBS)]
        got = [0] * A.PUB_SUBS

        def sub(k):
            def f():
                q, acc = qs[k], 0
                while True:
                    v = q.get()
                    if v is _STOP:
                        break
                    acc += v
                got[k] = acc
            return f

        def publisher():
            for i in range(msgs):
                for q in qs:
                    q.put(i)
            for q in qs:
                q.put(_STOP)
        self._join([sub(k) for k in range(A.PUB_SUBS)] + [publisher])
        if any(x != msgs * (msgs - 1) // 2 for x in got):
            raise AssertionError("pub/sub lost a message")

    def crawl(self, n):
        g = self.g
        acc = [0] * n

        def task(t):
            def f():
                a = 0
                for k in range(A.CRAWL_FETCHES):
                    g.sleep(A.crawl_latency_s(t, k))
                    a += A.crawl_parse(t, k)
                acc[t] = a
            return f
        self._join([task(t) for t in range(n)])
        if sum(acc) != A.crawl_expected(n):
            raise AssertionError("crawl checksum")

    def memory(self, s, counts):
        g = self.g
        for n in counts:
            ev = g.event.Event()
            ready = bytearray(n)

            def unit(k):
                ready[k] = 1
                ev.wait()
            gc.collect()
            before = A.rss_bytes()
            gs = [g.spawn(unit, k) for k in range(n)]
            _settle_until(ready, n, g.sleep)
            after = A.rss_bytes()
            s.memory(A.mem_row(n), n, before, after, note="greenlets parked on Event.wait")
            ev.set()
            g.joinall(gs, raise_error=True)


def make(kind, rt):
    """The apps runner for a bench.baselines runtime instance."""
    if kind == "threads":
        return ThreadsApps()
    if kind in ("asyncio", "uvloop"):
        return AsyncioApps(rt.loop)
    if kind == "trio":
        return TrioApps()
    return GeventApps()
