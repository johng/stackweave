"""Other Python runtimes on the bench.mnsched / bench.echo workloads.

The same workloads, under the same entry names and inner counts, on plain
`threading` (+ `queue`), `asyncio`, `uvloop`, `trio` and `gevent`, so their
rows line up with stackweave's in one table (bench.compare runs them all).
No stackweave is imported: run this on a STOCK interpreter.

  pingpong local-wake      one unbuffered hand-off pair (Queue(1) / Channel /
                           memory channel 0)
  spawn noop               start n no-op units and join them (threads: in
                           batches of 500); recorded under both stackweave
                           spawn rows
  yield 1000 fibers x200   os.sched_yield / sleep(0) / checkpoint
  64 pairs pingpong        128 units
  fan-out 1->32 buf64      queue(64) -> 32 workers -> queue(64) -> collector
  mutex 64 contended       the runtime's lock
  waitgroup fork-join      100 children per round, joined
  blocking() sleep(100us)  threads call it directly; the loops go through
                           their thread offload (run_in_executor,
                           to_thread.run_sync, the gevent threadpool)
  latencies                foreign-thread wake, unit->unit wake, spawn->first
                           run, 1 ms timer lateness
  py-echo                  64 persistent loopback conns x 500 x 64 B, server
                           and clients in-process; no hub count, so it is
                           recorded in the default-hubs row ("py-echo @4h")

Not here: select (no stdlib channel select), stackweave's pinned / busy /
drifted routing variants and hub scaling.  asyncio.Lock and gevent's
Semaphore never contend (no yield inside the critical section); trio.Lock
does (acquire is a checkpoint).

Run:
    PYTHONPATH=benchmark python -m bench.baselines --kind uvloop --out x.json
    ... --suite mnsched|echo|all  --quick
"""
import sys

# The harness probes stackweave_c for its env block; a baseline must not load
# an extension built for another interpreter.
sys.modules["stackweave_c"] = None

import argparse
import functools
import os
import socket
import threading
import time

from bench.gil import ensure_nogil
from bench.harness import Suite

HOST = "127.0.0.1"
MSG = 64
# No hub count here: the echo number goes in stackweave's default-hubs row.
ECHO_ROW = "py-echo @%sh" % os.environ.get("STACKWEAVE_BENCH_HUBS", "4")
KINDS = ("threads", "asyncio", "uvloop", "trio", "gevent")
SPAWN_ROWS = ("spawn noop mn_fiber", "spawn noop stackweave.fiber")
_STOP = object()


def _check(cond, msg):
    if not cond:
        raise RuntimeError(msg)


def _payload():
    return bytes(range(MSG))


def _blocking_call():
    time.sleep(0.0001)
    return 1


# ====================================================================
# threads
# ====================================================================
class Threads:
    def __init__(self):
        import queue
        self.Q = queue.Queue

    @staticmethod
    def all(targets):
        ts = [threading.Thread(target=t) for t in targets]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

    def pingpong(self, n):
        Q = self.Q

        def once():
            a, b = Q(1), Q(1)
            got = [0]

            def pinger():
                for i in range(n):
                    a.put(i)
                    b.get()

            def ponger():
                for _ in range(n):
                    b.put(a.get())
                    got[0] += 1
            self.all([pinger, ponger])
            _check(got[0] == n, "pingpong")
        return once

    def spawn(self, n, batch=500):
        def noop():
            pass

        def once():
            done = 0
            while done < n:
                k = min(batch, n - done)
                self.all([noop] * k)
                done += k
        return once

    def yield_(self, units, m):
        count = bytearray(units)

        def once():
            def mk(k):
                def w():
                    y = os.sched_yield
                    for _ in range(m):
                        y()
                    count[k] = 1
                return w
            for k in range(units):
                count[k] = 0
            self.all([mk(k) for k in range(units)])
            _check(sum(count) == units, "yield")
        return once

    def pairs(self, pairs, n):
        Q = self.Q
        done = bytearray(pairs)

        def once():
            fs = []
            for k in range(pairs):
                done[k] = 0
                a, b = Q(1), Q(1)

                def pinger(a=a, b=b):
                    for i in range(n):
                        a.put(i)
                        b.get()

                def ponger(a=a, b=b, k=k):
                    for _ in range(n):
                        b.put(a.get())
                    done[k] = 1
                fs += [pinger, ponger]
            self.all(fs)
            _check(sum(done) == pairs, "pairs")
        return once

    def fanout(self, items, workers, cap):
        Q = self.Q

        def once():
            work, acks = Q(cap), Q(cap)
            total = [0]

            def producer():
                for i in range(items):
                    work.put(i)
                for _ in range(workers):
                    work.put(_STOP)

            def worker():
                while True:
                    v = work.get()
                    if v is _STOP:
                        break
                    acks.put(v)

            def collector():
                s = 0
                for _ in range(items):
                    s += acks.get()
                total[0] = s
            self.all([producer, collector] + [worker] * workers)
            _check(total[0] == items * (items - 1) // 2, "fanout")
        return once

    def mutex(self, units, m):
        def once():
            mu = threading.Lock()
            cnt = [0]

            def w():
                for _ in range(m):
                    with mu:
                        cnt[0] += 1
            self.all([w] * units)
            _check(cnt[0] == units * m, "mutex")
        return once

    def waitgroup(self, rounds, width):
        def once():
            hit = bytearray(width)
            ok = 0
            for _ in range(rounds):
                def child(k):
                    def f():
                        hit[k] = 1
                    return f
                self.all([child(k) for k in range(width)])
                ok += sum(hit)
                for k in range(width):
                    hit[k] = 0
            _check(ok == rounds * width, "waitgroup")
        return once

    def blocking(self, callers, m):
        def once():
            per = [0] * callers

            def mk(k):
                def c():
                    s = 0
                    for _ in range(m):
                        s += _blocking_call()
                    per[k] = s
                return c
            self.all([mk(k) for k in range(callers)])
            _check(sum(per) == callers * m, "blocking")
        return once

    def lat_foreign(self, n, gap_s=0.0002):
        lat = []
        q = self.Q()

        def rx():
            for _ in range(n):
                v = q.get()
                lat.append(time.perf_counter_ns() - v)

        def tx():
            for _ in range(n):
                q.put(time.perf_counter_ns())
                time.sleep(gap_s)
        self.all([rx, tx])
        return lat

    lat_wake = None          # thread -> thread is lat_foreign

    def lat_spawn(self, n, gap_s=0.0003):
        lat = []
        for _ in range(n):
            t0 = time.perf_counter_ns()

            def f(t0=t0):
                lat.append(time.perf_counter_ns() - t0)
            t = threading.Thread(target=f)
            t.start()
            time.sleep(gap_s)
            t.join()
        return lat

    def lat_timer(self, units, rounds, sleep_s=0.001):
        lat = [0] * (units * rounds)

        def mk(k):
            def f():
                for r in range(rounds):
                    t0 = time.perf_counter_ns()
                    time.sleep(sleep_s)
                    lat[k * rounds + r] = max(0, time.perf_counter_ns() - t0 - int(sleep_s * 1e9))
            return f
        self.all([mk(k) for k in range(units)])
        return lat

    def echo(self, s, conns_n, rounds, samples, warmup):
        srv = socket.create_server((HOST, 0), backlog=1024)
        port = srv.getsockname()[1]
        handlers = []

        def handle(c):
            with c:
                while True:
                    d = c.recv(4096)
                    if not d:
                        break
                    c.sendall(d)

        def acceptor():
            for _ in range(conns_n):
                c, _ = srv.accept()
                c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                t = threading.Thread(target=handle, args=(c,), daemon=True)
                t.start()
                handlers.append(t)
        acc = threading.Thread(target=acceptor)
        acc.start()
        conns = []
        for _ in range(conns_n):
            c = socket.create_connection((HOST, port))
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conns.append(c)
        acc.join()
        payload = _payload()

        def once():
            bad = []

            def mk(c):
                def f():
                    buf = bytearray(MSG)
                    view = memoryview(buf)
                    try:
                        for _ in range(rounds):
                            c.sendall(payload)
                            got = 0
                            while got < MSG:
                                k = c.recv_into(view[got:])
                                if k == 0:
                                    raise ConnectionError("EOF")
                                got += k
                            if buf != payload:
                                raise AssertionError("echo mismatch")
                    except Exception as e:    # noqa: BLE001 -- reported below
                        bad.append(e)
                return f
            self.all([mk(c) for c in conns])
            _check(not bad, "echo: %r" % (bad[:1],))
        try:
            s.bench(ECHO_ROW, once, inner=conns_n * rounds,
                    note="1 server thread per conn + %d client threads" % conns_n)
        finally:
            for c in conns:
                c.close()
            for t in handlers:
                t.join()
            srv.close()


# ====================================================================
# asyncio / uvloop
# ====================================================================
class Asyncio:
    def __init__(self, uvloop=False):
        import asyncio
        self.aio = asyncio
        if uvloop:
            import uvloop as _uv
            self.loop = _uv.new_event_loop()
        else:
            self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def run(self, coro_fn):
        loop = self.loop

        def once():
            return loop.run_until_complete(coro_fn())
        return once

    def run_once(self, coro_fn):
        return self.run(coro_fn)()

    def pingpong(self, n):
        aio = self.aio

        async def go():
            a, b = aio.Queue(1), aio.Queue(1)
            got = [0]

            async def pinger():
                for i in range(n):
                    await a.put(i)
                    await b.get()

            async def ponger():
                for _ in range(n):
                    await b.put(await a.get())
                    got[0] += 1
            await aio.gather(pinger(), ponger())
            _check(got[0] == n, "pingpong")
        return self.run(go)

    def spawn(self, n):
        aio = self.aio

        async def noop():
            pass

        async def go():
            await aio.gather(*[aio.ensure_future(noop()) for _ in range(n)])
        return self.run(go)

    def yield_(self, units, m):
        aio = self.aio

        async def go():
            count = bytearray(units)

            async def w(k):
                for _ in range(m):
                    await aio.sleep(0)
                count[k] = 1
            await aio.gather(*(w(k) for k in range(units)))
            _check(sum(count) == units, "yield")
        return self.run(go)

    def pairs(self, pairs, n):
        aio = self.aio

        async def go():
            done = bytearray(pairs)
            cs = []
            for k in range(pairs):
                a, b = aio.Queue(1), aio.Queue(1)

                async def pinger(a=a, b=b):
                    for i in range(n):
                        await a.put(i)
                        await b.get()

                async def ponger(a=a, b=b, k=k):
                    for _ in range(n):
                        await b.put(await a.get())
                    done[k] = 1
                cs += [pinger(), ponger()]
            await aio.gather(*cs)
            _check(sum(done) == pairs, "pairs")
        return self.run(go)

    def fanout(self, items, workers, cap):
        aio = self.aio

        async def go():
            work, acks = aio.Queue(cap), aio.Queue(cap)
            total = [0]

            async def producer():
                for i in range(items):
                    await work.put(i)
                for _ in range(workers):
                    await work.put(_STOP)

            async def worker():
                while True:
                    v = await work.get()
                    if v is _STOP:
                        break
                    await acks.put(v)

            async def collector():
                s = 0
                for _ in range(items):
                    s += await acks.get()
                total[0] = s
            await aio.gather(producer(), collector(), *(worker() for _ in range(workers)))
            _check(total[0] == items * (items - 1) // 2, "fanout")
        return self.run(go)

    def mutex(self, units, m):
        aio = self.aio

        async def go():
            mu = aio.Lock()
            cnt = [0]

            async def w():
                for _ in range(m):
                    async with mu:
                        cnt[0] += 1
            await aio.gather(*(w() for _ in range(units)))
            _check(cnt[0] == units * m, "mutex")
        return self.run(go)

    def waitgroup(self, rounds, width):
        aio = self.aio

        async def go():
            hit = bytearray(width)
            ok = 0

            async def child(k):
                hit[k] = 1
            for _ in range(rounds):
                await aio.gather(*(child(k) for k in range(width)))
                ok += sum(hit)
                for k in range(width):
                    hit[k] = 0
            _check(ok == rounds * width, "waitgroup")
        return self.run(go)

    def blocking(self, callers, m):
        aio = self.aio

        async def go():
            loop = aio.get_running_loop()
            per = [0] * callers

            async def c(k):
                s = 0
                for _ in range(m):
                    s += await loop.run_in_executor(None, _blocking_call)
                per[k] = s
            await aio.gather(*(c(k) for k in range(callers)))
            _check(sum(per) == callers * m, "blocking")
        return self.run(go)

    def lat_foreign(self, n, gap_s=0.0002):
        aio = self.aio

        async def go():
            loop = aio.get_running_loop()
            q = aio.Queue()
            lat = []

            def tx():
                for _ in range(n):
                    loop.call_soon_threadsafe(q.put_nowait, time.perf_counter_ns())
                    time.sleep(gap_s)
            t = threading.Thread(target=tx)
            t.start()
            for _ in range(n):
                v = await q.get()
                lat.append(time.perf_counter_ns() - v)
            t.join()
            return lat
        return self.run_once(go)

    def lat_wake(self, n, gap_s=0.0002):
        aio = self.aio

        async def go():
            q = aio.Queue()
            lat = []

            async def tx():
                for _ in range(n):
                    q.put_nowait(time.perf_counter_ns())
                    await aio.sleep(gap_s)

            async def rx():
                for _ in range(n):
                    v = await q.get()
                    lat.append(time.perf_counter_ns() - v)
            await aio.gather(rx(), tx())
            return lat
        return self.run_once(go)

    def lat_spawn(self, n, gap_s=0.0003):
        aio = self.aio

        async def go():
            lat = []
            for _ in range(n):
                t0 = time.perf_counter_ns()

                async def f(t0=t0):
                    lat.append(time.perf_counter_ns() - t0)
                aio.ensure_future(f())
                await aio.sleep(gap_s)
            return lat
        return self.run_once(go)

    def lat_timer(self, units, rounds, sleep_s=0.001):
        aio = self.aio

        async def go():
            lat = [0] * (units * rounds)

            async def f(k):
                for r in range(rounds):
                    t0 = time.perf_counter_ns()
                    await aio.sleep(sleep_s)
                    lat[k * rounds + r] = max(0, time.perf_counter_ns() - t0 - int(sleep_s * 1e9))
            await aio.gather(*(f(k) for k in range(units)))
            return lat
        return self.run_once(go)

    def echo(self, s, conns_n, rounds, samples, warmup):
        aio = self.aio
        payload = _payload()

        async def handle(r, w):
            try:
                while True:
                    d = await r.read(4096)
                    if not d:
                        break
                    w.write(d)
                    await w.drain()
            finally:
                w.close()

        async def setup():
            srv = await aio.start_server(handle, HOST, 0, backlog=1024)
            port = srv.sockets[0].getsockname()[1]
            conns = []
            for _ in range(conns_n):
                r, w = await aio.open_connection(HOST, port)
                w.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conns.append((r, w))
            return srv, conns

        srv, conns = self.loop.run_until_complete(setup())

        async def client(r, w):
            for _ in range(rounds):
                w.write(payload)
                await w.drain()
                if await r.readexactly(MSG) != payload:
                    raise AssertionError("echo mismatch")

        async def all_clients():
            await aio.gather(*(client(r, w) for r, w in conns))

        async def close():
            for _, w in conns:
                w.close()
            srv.close()
            await srv.wait_closed()
        try:
            s.bench(ECHO_ROW, self.run(all_clients), inner=conns_n * rounds,
                    note="streams server + %d client tasks, one loop" % conns_n)
        finally:
            self.loop.run_until_complete(close())


# ====================================================================
# trio -- no persistent loop: one trio.run per sample (~0.3 ms, under 1%
# of every sample here); echo times its samples inside one trio.run.
# ====================================================================
class Trio:
    def __init__(self):
        import trio
        self.trio = trio

    def run(self, coro_fn):
        trio = self.trio

        def once():
            return trio.run(coro_fn)
        return once

    def run_once(self, coro_fn):
        return self.trio.run(coro_fn)

    @staticmethod
    def _pp(a_s, a_r, b_s, b_r, n, done, k):
        async def pinger():
            for i in range(n):
                await a_s.send(i)
                await b_r.receive()

        async def ponger():
            for _ in range(n):
                await b_s.send(await a_r.receive())
            done[k] = 1
        return pinger, ponger

    def pingpong(self, n):
        return self.pairs(1, n)

    def spawn(self, n):
        trio = self.trio

        async def noop():
            pass

        async def go():
            async with trio.open_nursery() as nur:
                for _ in range(n):
                    nur.start_soon(noop)
        return self.run(go)

    def yield_(self, units, m):
        trio = self.trio

        async def go():
            count = bytearray(units)
            cp = trio.lowlevel.checkpoint

            async def w(k):
                for _ in range(m):
                    await cp()
                count[k] = 1
            async with trio.open_nursery() as nur:
                for k in range(units):
                    nur.start_soon(w, k)
            _check(sum(count) == units, "yield")
        return self.run(go)

    def pairs(self, pairs, n):
        trio = self.trio

        async def go():
            done = bytearray(pairs)
            async with trio.open_nursery() as nur:
                for k in range(pairs):
                    a_s, a_r = trio.open_memory_channel(0)
                    b_s, b_r = trio.open_memory_channel(0)
                    p, q = self._pp(a_s, a_r, b_s, b_r, n, done, k)
                    nur.start_soon(p)
                    nur.start_soon(q)
            _check(sum(done) == pairs, "pairs")
        return self.run(go)

    def fanout(self, items, workers, cap):
        trio = self.trio

        async def go():
            w_s, w_r = trio.open_memory_channel(cap)
            a_s, a_r = trio.open_memory_channel(cap)
            total = [0]

            async def producer():
                for i in range(items):
                    await w_s.send(i)
                for _ in range(workers):
                    await w_s.send(_STOP)

            async def worker():
                while True:
                    v = await w_r.receive()
                    if v is _STOP:
                        break
                    await a_s.send(v)

            async def collector():
                s = 0
                for _ in range(items):
                    s += await a_r.receive()
                total[0] = s
            async with trio.open_nursery() as nur:
                nur.start_soon(producer)
                nur.start_soon(collector)
                for _ in range(workers):
                    nur.start_soon(worker)
            _check(total[0] == items * (items - 1) // 2, "fanout")
        return self.run(go)

    def mutex(self, units, m):
        trio = self.trio

        async def go():
            mu = trio.Lock()
            cnt = [0]

            async def w():
                for _ in range(m):
                    async with mu:
                        cnt[0] += 1
            async with trio.open_nursery() as nur:
                for _ in range(units):
                    nur.start_soon(w)
            _check(cnt[0] == units * m, "mutex")
        return self.run(go)

    def waitgroup(self, rounds, width):
        trio = self.trio

        async def go():
            hit = bytearray(width)
            ok = 0

            async def child(k):
                hit[k] = 1
            for _ in range(rounds):
                async with trio.open_nursery() as nur:
                    for k in range(width):
                        nur.start_soon(child, k)
                ok += sum(hit)
                for k in range(width):
                    hit[k] = 0
            _check(ok == rounds * width, "waitgroup")
        return self.run(go)

    def blocking(self, callers, m):
        trio = self.trio

        async def go():
            per = [0] * callers

            async def c(k):
                s = 0
                for _ in range(m):
                    s += await trio.to_thread.run_sync(_blocking_call)
                per[k] = s
            async with trio.open_nursery() as nur:
                for k in range(callers):
                    nur.start_soon(c, k)
            _check(sum(per) == callers * m, "blocking")
        return self.run(go)

    def lat_foreign(self, n, gap_s=0.0002):
        trio = self.trio

        async def go():
            token = trio.lowlevel.current_trio_token()
            snd, rcv = trio.open_memory_channel(float("inf"))
            lat = []

            def tx():
                for _ in range(n):
                    token.run_sync_soon(snd.send_nowait, time.perf_counter_ns())
                    time.sleep(gap_s)
            t = threading.Thread(target=tx)
            t.start()
            for _ in range(n):
                v = await rcv.receive()
                lat.append(time.perf_counter_ns() - v)
            t.join()
            return lat
        return self.run_once(go)

    def lat_wake(self, n, gap_s=0.0002):
        trio = self.trio

        async def go():
            snd, rcv = trio.open_memory_channel(float("inf"))
            lat = []

            async def tx():
                for _ in range(n):
                    snd.send_nowait(time.perf_counter_ns())
                    await trio.sleep(gap_s)

            async def rx():
                for _ in range(n):
                    v = await rcv.receive()
                    lat.append(time.perf_counter_ns() - v)
            async with trio.open_nursery() as nur:
                nur.start_soon(rx)
                nur.start_soon(tx)
            return lat
        return self.run_once(go)

    def lat_spawn(self, n, gap_s=0.0003):
        trio = self.trio

        async def go():
            lat = []
            async with trio.open_nursery() as nur:
                for _ in range(n):
                    t0 = time.perf_counter_ns()

                    async def f(t0=t0):
                        lat.append(time.perf_counter_ns() - t0)
                    nur.start_soon(f)
                    await trio.sleep(gap_s)
            return lat
        return self.run_once(go)

    def lat_timer(self, units, rounds, sleep_s=0.001):
        trio = self.trio

        async def go():
            lat = [0] * (units * rounds)

            async def f(k):
                for r in range(rounds):
                    t0 = time.perf_counter_ns()
                    await trio.sleep(sleep_s)
                    lat[k * rounds + r] = max(0, time.perf_counter_ns() - t0 - int(sleep_s * 1e9))
            async with trio.open_nursery() as nur:
                for k in range(units):
                    nur.start_soon(f, k)
            return lat
        return self.run_once(go)

    def echo(self, s, conns_n, rounds, samples, warmup):
        trio = self.trio
        payload = _payload()

        async def handle(stream):
            async with stream:
                while True:
                    d = await stream.receive_some(4096)
                    if not d:
                        break
                    await stream.send_all(d)

        async def client(st):
            for _ in range(rounds):
                await st.send_all(payload)
                got = b""
                while len(got) < MSG:
                    d = await st.receive_some(MSG - len(got))
                    if not d:
                        raise ConnectionError("EOF")
                    got += d
                if got != payload:
                    raise AssertionError("echo mismatch")

        async def go():
            async with trio.open_nursery() as nur:
                listeners = await nur.start(
                    functools.partial(trio.serve_tcp, handle, 0, host=HOST))
                port = listeners[0].socket.getsockname()[1]
                conns = [await trio.open_tcp_stream(HOST, port) for _ in range(conns_n)]
                for st in conns:
                    st.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                times = []
                for i in range(warmup + samples):
                    t0 = time.perf_counter_ns()
                    async with trio.open_nursery() as cn:
                        for st in conns:
                            cn.start_soon(client, st)
                    if i >= warmup:
                        times.append((time.perf_counter_ns() - t0) / 1e9)
                for st in conns:
                    await st.aclose()
                nur.cancel_scope.cancel()
            return times
        s.record(ECHO_ROW, trio.run(go), inner=conns_n * rounds,
                 note="serve_tcp + %d client tasks, one trio.run" % conns_n)


# ====================================================================
# gevent -- greenlets driven from the main greenlet; no monkey-patching,
# gevent.socket / gevent.queue used directly.
# ====================================================================
class Gevent:
    def __init__(self):
        import gevent
        import gevent.lock
        import gevent.queue
        self.g = gevent

    def _join(self, gs):
        self.g.joinall(gs, raise_error=True)

    def pingpong(self, n):
        return self.pairs(1, n)

    def spawn(self, n):
        g = self.g

        def noop():
            pass

        def once():
            self._join([g.spawn(noop) for _ in range(n)])
        return once

    def yield_(self, units, m):
        g = self.g
        count = bytearray(units)

        def once():
            def w(k):
                for _ in range(m):
                    g.sleep(0)
                count[k] = 1
            for k in range(units):
                count[k] = 0
            self._join([g.spawn(w, k) for k in range(units)])
            _check(sum(count) == units, "yield")
        return once

    def pairs(self, pairs, n):
        g = self.g
        done = bytearray(pairs)

        def once():
            gs = []
            for k in range(pairs):
                done[k] = 0
                a, b = g.queue.Channel(), g.queue.Channel()

                def pinger(a=a, b=b):
                    for i in range(n):
                        a.put(i)
                        b.get()

                def ponger(a=a, b=b, k=k):
                    for _ in range(n):
                        b.put(a.get())
                    done[k] = 1
                gs += [g.spawn(pinger), g.spawn(ponger)]
            self._join(gs)
            _check(sum(done) == pairs, "pairs")
        return once

    def fanout(self, items, workers, cap):
        g = self.g

        def once():
            work, acks = g.queue.Queue(cap), g.queue.Queue(cap)
            total = [0]

            def producer():
                for i in range(items):
                    work.put(i)
                for _ in range(workers):
                    work.put(_STOP)

            def worker():
                while True:
                    v = work.get()
                    if v is _STOP:
                        break
                    acks.put(v)

            def collector():
                s = 0
                for _ in range(items):
                    s += acks.get()
                total[0] = s
            self._join([g.spawn(producer), g.spawn(collector)]
                       + [g.spawn(worker) for _ in range(workers)])
            _check(total[0] == items * (items - 1) // 2, "fanout")
        return once

    def mutex(self, units, m):
        g = self.g

        def once():
            mu = g.lock.Semaphore(1)
            cnt = [0]

            def w():
                for _ in range(m):
                    with mu:
                        cnt[0] += 1
            self._join([g.spawn(w) for _ in range(units)])
            _check(cnt[0] == units * m, "mutex")
        return once

    def waitgroup(self, rounds, width):
        g = self.g

        def once():
            hit = bytearray(width)
            ok = 0

            def child(k):
                hit[k] = 1
            for _ in range(rounds):
                self._join([g.spawn(child, k) for k in range(width)])
                ok += sum(hit)
                for k in range(width):
                    hit[k] = 0
            _check(ok == rounds * width, "waitgroup")
        return once

    def blocking(self, callers, m):
        g = self.g

        def once():
            pool = g.get_hub().threadpool
            per = [0] * callers

            def c(k):
                s = 0
                for _ in range(m):
                    s += pool.apply(_blocking_call)
                per[k] = s
            self._join([g.spawn(c, k) for k in range(callers)])
            _check(sum(per) == callers * m, "blocking")
        return once

    def lat_foreign(self, n, gap_s=0.0002):
        g = self.g
        q = g.queue.Queue()
        loop = g.get_hub().loop
        lat = []

        def tx():
            for _ in range(n):
                loop.run_callback_threadsafe(q.put_nowait, time.perf_counter_ns())
                time.sleep(gap_s)
        # The hub raises LoopExit when nothing it knows about can wake the
        # waiter; a parked sleeper keeps it waiting for the foreign callback.
        keep = g.spawn(g.sleep, 3600)
        t = threading.Thread(target=tx)
        t.start()
        for _ in range(n):
            v = q.get()
            lat.append(time.perf_counter_ns() - v)
        t.join()
        keep.kill()
        return lat

    def lat_wake(self, n, gap_s=0.0002):
        g = self.g
        q = g.queue.Queue()
        lat = []

        def tx():
            for _ in range(n):
                q.put_nowait(time.perf_counter_ns())
                g.sleep(gap_s)

        def rx():
            for _ in range(n):
                v = q.get()
                lat.append(time.perf_counter_ns() - v)
        self._join([g.spawn(rx), g.spawn(tx)])
        return lat

    def lat_spawn(self, n, gap_s=0.0003):
        g = self.g
        lat = []
        for _ in range(n):
            t0 = time.perf_counter_ns()

            def f(t0=t0):
                lat.append(time.perf_counter_ns() - t0)
            g.spawn(f)
            g.sleep(gap_s)
        return lat

    def lat_timer(self, units, rounds, sleep_s=0.001):
        g = self.g
        lat = [0] * (units * rounds)

        def f(k):
            for r in range(rounds):
                t0 = time.perf_counter_ns()
                g.sleep(sleep_s)
                lat[k * rounds + r] = max(0, time.perf_counter_ns() - t0 - int(sleep_s * 1e9))
        self._join([g.spawn(f, k) for k in range(units)])
        return lat

    def echo(self, s, conns_n, rounds, samples, warmup):
        g = self.g
        from gevent import socket as gsocket
        from gevent.server import StreamServer
        payload = _payload()

        def handle(c, _addr):
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            while True:
                d = c.recv(4096)
                if not d:
                    break
                c.sendall(d)

        srv = StreamServer((HOST, 0), handle, backlog=1024)
        srv.start()
        conns = []
        for _ in range(conns_n):
            c = gsocket.create_connection((HOST, srv.server_port))
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conns.append(c)

        def client(c):
            buf = bytearray(MSG)
            view = memoryview(buf)
            for _ in range(rounds):
                c.sendall(payload)
                got = 0
                while got < MSG:
                    k = c.recv_into(view[got:])
                    if k == 0:
                        raise ConnectionError("EOF")
                    got += k
                if buf != payload:
                    raise AssertionError("echo mismatch")

        def once():
            self._join([g.spawn(client, c) for c in conns])
        try:
            s.bench(ECHO_ROW, once, inner=conns_n * rounds,
                    note="StreamServer + %d client greenlets" % conns_n)
        finally:
            for c in conns:
                c.close()
            srv.stop()


# --------------------------------------------------------------------
def make(kind):
    return {"threads": Threads, "asyncio": Asyncio,
            "uvloop": lambda: Asyncio(uvloop=True),
            "trio": Trio, "gevent": Gevent}[kind]()


def main(argv=None):
    ensure_nogil()
    ap = argparse.ArgumentParser(description="other Python runtimes on the stackweave workloads")
    ap.add_argument("--kind", choices=KINDS, required=True)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--suite", choices=("mnsched", "echo", "all"), default="all")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    q = 10 if args.quick else 1
    n = 400 if args.quick else 4_000
    samples, warmup = (3, 1) if args.quick else (12, 3)
    s = Suite("baseline-" + args.kind, pin_cpus=[], samples=samples, warmup=warmup)
    s.env["runtime"] = args.kind
    rt = make(args.kind)
    print("runtime: %s  python %s  gil=%s\n" % (args.kind, s.env["python"], s.env["gil_enabled"]))

    if args.suite in ("mnsched", "all"):
        s.bench("pingpong local-wake", rt.pingpong(100_000 // q), inner=100_000 // q)
        st = s.bench(SPAWN_ROWS[0], rt.spawn(20_000 // q), inner=20_000 // q)
        s.record(SPAWN_ROWS[1], st["raw_s"], inner=20_000 // q, note="same run as the row above")
        s.bench("yield 1000 fibers x200", rt.yield_(1_000, 200 // q), inner=1_000 * (200 // q))
        s.bench("64 pairs pingpong", rt.pairs(64, 2_000 // q), inner=64 * (2_000 // q))
        s.bench("fan-out 1->32 buf64", rt.fanout(100_000 // q, 32, 64), inner=100_000 // q)
        s.bench("mutex 64 fibers contended", rt.mutex(64, 2_000 // q), inner=64 * (2_000 // q))
        wg = 50 // min(q, 5)
        s.bench("waitgroup fork-join 100x50", rt.waitgroup(wg, 100), inner=wg * 100)
        s.bench("blocking() sleep(100us)", rt.blocking(32, 100 // q), inner=32 * (100 // q))
        s.latency("foreign thread -> fiber wake", rt.lat_foreign(n))
        if rt.lat_wake is not None:
            s.latency("cross-hub pinned wake", rt.lat_wake(n))
        s.latency("spawn->run round-robin", rt.lat_spawn(n // 4))
        s.latency("timer lateness 1ms", rt.lat_timer(200 if args.quick else 1_000, 10))

    if args.suite in ("echo", "all"):
        rt.echo(s, 64, max(10, 500 // q), samples, warmup)
    s.write(args.out)


if __name__ == "__main__":
    main()
