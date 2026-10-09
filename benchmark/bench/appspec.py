"""Shared definition of the application-shaped workloads (bench.apps) and the
memory probe (bench.memory), so every runtime runs the same program.

stackweave (apps.py / memory.py), the other Python runtimes (baselines.py)
import this; gobench/main.go mirrors the constants (keep them in step).
Stdlib only: the baselines run on a stock interpreter.

  http json api   64 keep-alive clients x 200 requests: an HTTP/1.1 GET
                  /user/<id> is parsed (request line + headers), routed, and
                  answered with a JSON body + Content-Length; the client
                  parses the response and checks the decoded id.
  api gateway     2000 requests, 100 in flight; each request fans out to 8
                  backend calls (1 ms of latency + a little work) and joins
                  them -- spawn, timers and join under concurrency.
  parse+hash      20000 JSON records through producer -> 4 workers
                  (json.loads + sha256) -> aggregator: CPU-bound work over
                  channels, where only a parallel runtime can scale.
  pub/sub         1 publisher broadcasts 500 messages to 256 subscribers,
                  each behind its own 16-slot queue: wake storms.
  crawler         10000 concurrent tasks x 3 fetches of 1-5 ms latency each:
                  lots of mostly-waiting tasks and timers at once.

  memory          RSS growth per parked unit (fiber / thread / task /
                  greenlet / goroutine) at 10k and 100k, measured from the OS.
"""
import json
import os
import subprocess

HTTP_CONNS, HTTP_REQS = 64, 200
GW_REQUESTS, GW_INFLIGHT, GW_FANOUT, GW_LATENCY_S = 2000, 100, 8, 0.001
PIPE_RECORDS, PIPE_WORKERS, PIPE_CAP = 20000, 4, 256
PUB_SUBS, PUB_MSGS, PUB_CAP = 256, 500, 16
CRAWL_TASKS, CRAWL_FETCHES = 10000, 3
MEM_COUNTS = (10_000, 100_000)
MEM_THREADS_MAX = 10_000            # 100k OS threads is not a real configuration

ROWS = {
    "http": "http json api 64 conns",
    "gateway": "api gateway fan-out 8 x 1ms",
    "pipeline": "parse+hash pipeline 4 workers",
    "pubsub": "pub/sub broadcast 256 subs",
    "crawl": "crawler 10k concurrent x 3 fetches",
}


def scaled(quick):
    """Per-sample sizes; --quick divides the long dimension by 10."""
    q = 10 if quick else 1
    return dict(http_reqs=max(10, HTTP_REQS // q), gw_requests=GW_REQUESTS // q,
                pipe_records=PIPE_RECORDS // q, pub_msgs=PUB_MSGS // q,
                crawl_tasks=CRAWL_TASKS // q)


# ---------------------------------------------------------------- http
def http_request(uid):
    return (b"GET /user/%d HTTP/1.1\r\nHost: bench\r\nUser-Agent: swbench/1\r\n"
            b"Accept: application/json\r\n\r\n" % uid)


def http_handle(head):
    """Server side: parse one request head, route it, return the response."""
    lines = head.split(b"\r\n")
    method, path, _ = lines[0].split(b" ", 2)
    headers = {}
    for ln in lines[1:]:
        if ln:
            k, _, v = ln.partition(b":")
            headers[k.strip().lower()] = v.strip()
    if method != b"GET" or not path.startswith(b"/user/") or b"host" not in headers:
        return b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
    uid = int(path[6:])
    body = json.dumps({"id": uid, "name": "user-%d" % uid, "tags": ["a", "b", "c"],
                       "score": uid * 7 % 1000}).encode()
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\n\r\n" % len(body)) + body


def http_check(head, body, uid):
    if not head.startswith(b"HTTP/1.1 200"):
        raise AssertionError("http status: %r" % head[:40])
    if json.loads(body)["id"] != uid:
        raise AssertionError("http body id mismatch")


def content_length(head):
    for ln in head.split(b"\r\n")[1:]:
        k, _, v = ln.partition(b":")
        if k.strip().lower() == b"content-length":
            return int(v)
    return 0


class Framer:
    """Blocking-style HTTP framing over any recv(n) -> bytes (b"" = EOF).
    Used by the runtimes whose sockets block the unit (threads, stackweave,
    gevent); the async runtimes use their stream readers instead."""

    def __init__(self, recv):
        self.recv = recv
        self.buf = b""

    def head(self):
        while True:
            i = self.buf.find(b"\r\n\r\n")
            if i >= 0:
                h, self.buf = self.buf[:i], self.buf[i + 4:]
                return h
            d = self.recv(65536)
            if not d:
                return None
            self.buf += d

    def body(self, n):
        while len(self.buf) < n:
            d = self.recv(65536)
            if not d:
                raise ConnectionError("EOF in body")
            self.buf += d
        b, self.buf = self.buf[:n], self.buf[n:]
        return b


class AsyncFramer(Framer):
    """Framer over an async recv(n) (asyncio StreamReader.read, trio
    receive_some): the same parsing, awaited."""

    async def head(self):
        while True:
            i = self.buf.find(b"\r\n\r\n")
            if i >= 0:
                h, self.buf = self.buf[:i], self.buf[i + 4:]
                return h
            d = await self.recv(65536)
            if not d:
                return None
            self.buf += d

    async def body(self, n):
        while len(self.buf) < n:
            d = await self.recv(65536)
            if not d:
                raise ConnectionError("EOF in body")
            self.buf += d
        b, self.buf = self.buf[:n], self.buf[n:]
        return b


def uid_of(conn_idx, i):
    return conn_idx * 100_000 + i


# ---------------------------------------------------------------- gateway
def backend_work(req, k):
    """What a backend call returns after its latency: a little real work."""
    return sum(range(100)) + req * GW_FANOUT + k


def gateway_expected(n_requests):
    return sum(backend_work(r, k) for r in range(n_requests) for k in range(GW_FANOUT))


# ---------------------------------------------------------------- pipeline
_RECORDS = {}


def pipe_records(n):
    """n JSON-encoded records, built once (untimed)."""
    if n not in _RECORDS:
        _RECORDS[n] = [json.dumps({"id": i, "user": "u%d" % i, "payload": "x" * 200,
                                   "values": list(range(i % 50, i % 50 + 10))}).encode()
                       for i in range(n)]
    return _RECORDS[n]


def pipe_work(rec):
    """json.loads + sha256: what each worker does to one record."""
    import hashlib
    d = json.loads(rec)
    h = hashlib.sha256((d["user"] + d["payload"]).encode()).digest()
    return sum(d["values"]) + h[0]


_PIPE_EXPECT = {}


def pipe_expected(n):
    if n not in _PIPE_EXPECT:
        _PIPE_EXPECT[n] = sum(pipe_work(r) for r in pipe_records(n))
    return _PIPE_EXPECT[n]


# ---------------------------------------------------------------- crawler
def crawl_latency_s(task, fetch):
    return (1 + (task * 7 + fetch) % 5) / 1000.0


def crawl_parse(task, fetch):
    return len(str(task * 31 + fetch))


def crawl_expected(n_tasks):
    return sum(crawl_parse(t, f) for t in range(n_tasks) for f in range(CRAWL_FETCHES))


# ---------------------------------------------------------------- memory
def rss_bytes():
    """Current resident set size of this process, from the OS."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except OSError:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True).stdout
        return int(out.strip()) * 1024


def mem_row(n):
    return "rss per parked unit @%dk" % (n // 1000)

