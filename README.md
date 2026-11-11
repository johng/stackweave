# Stackweave

> [!WARNING]
> **Stackweave is an experimental fork of [runloom](https://github.com/robertsdotpm/runloom)**
> by Matthew Roberts. It exists to try out new ideas before they are proposed
> for merging into upstream runloom. Expect unstable APIs, half-finished
> experiments, and changes that may never land upstream. For the maintained
> project, use [runloom](https://github.com/robertsdotpm/runloom). Stackweave
> remains MIT-licensed.

Go-style stackful coroutines for Python. Write **blocking** code — `fiber(fn)`,
plain `recv`/`send`, no `async`/`await` — and run a million of them across every
core in one process. Hand-rolled asm context switch + C work-stealing scheduler +
netpoll, built for **free-threaded Python 3.14t** (GIL off).

```python
import threading, stackweave
from urllib.request import urlopen
stackweave.monkey.patch()

def crawl(url):
    # urlopen() looks blocking -- but monkey.patch() parks the goroutine on the
    # socket instead of the OS thread, so all 64 fetches overlap on real cores.
    body = urlopen(url, timeout=10).read()
    print(threading.get_native_id(), len(body))

def main():
    for _ in range(64):
        stackweave.fiber(crawl, "http://example.com")

stackweave.run(8, main)   # 8 hub threads -> real cores on 3.14t (GIL off)
```

## Stackweave vs Go and the Python runtimes

One set of workloads, run on stackweave and on threads, asyncio, uvloop, trio,
gevent and Go (`benchmark/bench`, `python -m bench.compare`). DigitalOcean c-32
(32-core Xeon 8358, kernel 7.0), every CPython 3.14.4 built the same way
(clang-19, PGO, no LTO), median of 3 interleaved passes. Every runtime gets the
same parallelism: 4 hubs, `GOMAXPROCS=4`, and the Python runtimes pinned to 4
CPUs, unless a row says otherwise.[^cores] Higher is better unless marked.

| workload | stackweave | best Python runtime | Go |
| --- | ---: | ---: | ---: |
| ping-pong, one unbuffered pair (round-trips/s) | 849 k | 197 k (uvloop, GIL build) | 2.74 M |
| 64 ping-pong pairs (round-trips/s) | 3.47 M | 266 k (uvloop, GIL build) | 4.68 M |
| yield, 1000 fibers (switches/s) | **9.20 M** | 1.49 M (uvloop, GIL build) | 2.90 M |
| fan-out 1→32 over a buffered channel (items/s) | 1.33 M | 527 k (uvloop, GIL build) | 4.41 M |
| spawn a no-op fiber (/s) | 130–273 k | 403 k (uvloop, GIL build) | 2.89 M |
| TCP echo, Python handler, io_uring loop, 4 / 16 hubs (round-trips/s) | 289 k / 862 k | 164 k / 490 k (thread per conn) | 305 k / 946 k |
| HTTP/1.1 JSON API, 64 keep-alive conns (req/s) | 93 k (127 k on the io_uring loop) | 84 k (thread per conn) | 74 k (net/http) |
| API gateway: 8 × 1 ms backend calls per request (req/s) | 29.6 k | 21.1 k (uvloop, GIL build) | 70 k |
| JSON parse + sha256 pipeline, 4 workers (records/s) | 456 k | 167 k (gevent) | 690 k |
| pub/sub broadcast to 256 subscribers (deliveries/s) | 4.80 M | 1.36 M (gevent) | 5.45 M |
| crawler: 10k concurrent tasks × 3 slow fetches (fetches/s) | 98 k | 224 k (uvloop, GIL build) | 1.65 M |
| memory per parked fiber at 100k (lower is better) | 24 KB | 1.0 KB (uvloop) | 2.4 KB |
| foreign thread → fiber wake, p50 / p99 (lower is better) | **12 / 18 µs** | 14 / 28 µs (threads) | — |
| 1 ms timer lateness, p50, io_uring loop (lower is better) | **8.5 µs** | 65 µs (threads) | 331 µs |

[^cores]: Equal parallelism, not the whole box, and not identical CPU masks.
    stackweave runs every fiber on its 4 hub threads (16 in the 16-hub echo
    cells); the harness pins its process to more CPUs than that (8 for the app
    rows, 16 or 32 for the others) so its helper threads -- sysmon, the
    `blocking()` pool -- do not take a hub's core. Go runs with `GOMAXPROCS=4`
    (16), which bounds the threads running Go code but not those in syscalls
    or GC background work, and is not pinned. The threads and event-loop
    processes are pinned (`sched_setaffinity`) to 4 CPUs, 16 in the 16-hub echo
    cells. The memory rows are not pinned (RSS does not depend on it).
    asyncio, uvloop, trio and gevent run their loop on one thread either way;
    the pin mainly holds back threads, which would otherwise spread a thread
    per connection over all 32 cores. Unpinned in an
    [earlier run](benchmark/bench/results/compare/20261109-070826-linux-c32/summary.md)
    (Xeon 8280), threads served the HTTP row at 239 k req/s. This table
    compares runtimes at the same parallelism, not how far each one scales on
    a bigger box.

The short story: stackweave runs message-passing workloads 2.5–13× faster than
the fastest Python event loop, runs a CPU-bound pipeline 2.7× faster than any
of them (3.3× faster than threads on 4 CPUs), out-yields Go 3×, and has
the tightest wakes and timers here. On the HTTP API it is 27% ahead of Go and
11% ahead of a thread per connection (52% on the io_uring loop), and Python
echo on the io_uring loop is within 5–9% of Go at 4, 8 and 16 hubs. Go is
still 1.35–3.3× ahead on channel hand-offs, and far ahead on spawn and on
timer-heavy fan-out: 2.4× on the gateway row, and the event loops beat
stackweave on the crawler. The honest gaps are memory (a parked fiber carries
a C stack and a thread state, ~10× a goroutine), spawn / sleep cost at 10k+
fibers, and lock contention: 64 fibers on one `Mutex` run 3.1× slower than 64
threads on a `threading.Lock`. Every row and caveat:
**[latest comparison](benchmark/bench/results/compare/20261111-074726-linux-c32/summary.md)**.

```python
stackweave.optimize("throughput")   # max spawn rate + a 16-worker blocking() pool
stackweave.optimize("memory")       # grow-down sizer: smaller stack reservations (opt-in)
```

## Install

stackweave installs only onto a free-threaded CPython built with its migration
patches — see [src/patches/](src/patches/README.md) (`tools/ci/build_patched_cpython.sh 3.14.4`
builds one). `pip install` refuses a stock interpreter. stackweave is not on PyPI
yet, so install it from GitHub (or `pip install -e .` from a clone) with the
patched interpreter's pip:

```bash
/path/to/patched/bin/python3.14 -m pip install git+https://github.com/johng/stackweave
```

```python
import stackweave      # scheduler + channels, plus monkey/time/context/sync/aio
```

pip builds it from source (needs a C compiler): the patched and stock
interpreters share the `cp3NNt` wheel tag, so a prebuilt wheel couldn't be kept
off stock CPython. **No runtime dependencies.**

## What it is

- **Hand-rolled asm context switch** (x86_64 SysV, aarch64) — ~80 ns/swap, no
  syscall; POSIX `ucontext` fallback.
- **M:N work-stealing scheduler** — Chase-Lev deque per hub, per-hub MPSC
  submission; a woken goroutine lands on its waker's deque (Go-style local
  wake) or a global run-queue and resumes on whichever hub takes it.
- **Per-goroutine `PyThreadState` snapshot** — cframe, datastack, exc_info,
  contextvars, recursion; a million yielded goroutines share their hub threads
  with no frame-chain cliff.
- **netpoll** — epoll / kqueue / select; goroutines park
  transparently on fd readiness, lost-wake-free 3-state park-commit.
- **Go-style channels** — `Chan(capacity)`, `select`, `for v in ch`.
- **Stall isolation + recovery** — one unanticipated blocking call stalls only
  its hub, and the runtime detects + recovers it (default on).
- **`monkey.patch()`** makes blocking stdlib (`socket`, `time`, `threading`, …)
  cooperative, so existing blocking code runs unchanged.

Already have `async def` code? The **`stackweave.aio`** bridge runs it on the
single-threaded scheduler (`stackweave.aio.run(main())` ≈ `asyncio.run`) — a
zero-rewrite port path, not a multi-core speedup (use the sync API with
`run(n>1, main)` for that).

## Honest limitations

- **Free-threaded CPython 3.14+ only.** `setup.py` refuses GIL builds and older
  versions. With the GIL re-enabled at runtime (`PYTHON_GIL=1`) stackweave still
  runs single-hub — cheap spawn, the goroutine model, netpoll — but single-core
  like asyncio.
- **stackweave doesn't make Python faster per core.** CPython's ~80 k pure-Python
  ops/s/core is a constant it can't raise; it lets one process hit that on every
  core at once with a blocking model. The scheduler itself is Go-class.
- **Higher memory per goroutine than Go** (~3.3× for an empty fiber — the CPython
  eval frame; a C handler closes most of it).
- **Preemption fires only at Python bytecode boundaries** — a goroutine inside a
  tight pure-C call (e.g. `numpy`) holds its hub until it returns (same as Go +
  cgo).
- **Linux x86_64 is the primary, heavily-validated target** (2 M-conn
  runs, fuzzing, sanitizers, formal models — mostly on 3.13t, before 3.14t
  became the only supported version); other backends are maintained
  in-step but less deeply exercised.

## Platform support

| OS / arch | switch | netpoll | tested |
| --- | --- | --- | --- |
| Linux x86_64 | fcontext-asm | epoll | **yes — hw, 3.14t (primary)** |
| Linux aarch64 | fcontext-asm | epoll | qemu |
| macOS x86_64 / arm64 | fcontext-asm | kqueue | hw, 3.14t |
| FreeBSD / GhostBSD | fcontext-asm | kqueue | hw on 3.12 only — not yet re-validated on 3.14t |
| Solaris / Android / other BSD | ucontext / asm | select / epoll / kqueue | review |

## Docs & layout

Full guide in [docs/](https://github.com/johng/stackweave/tree/main/docs/):
[Quickstart](https://github.com/johng/stackweave/blob/main/docs/quickstart.md) ·
[Asyncio bridge](https://github.com/johng/stackweave/blob/main/docs/asyncio.md) ·
[Sync API](https://github.com/johng/stackweave/blob/main/docs/sync-api.md) ·
[Channels](https://github.com/johng/stackweave/blob/main/docs/channels.md) ·
[M:N parallelism](https://github.com/johng/stackweave/blob/main/docs/parallelism.md) ·
[Cookbook](https://github.com/johng/stackweave/blob/main/docs/cookbook.md) ·
[API reference](https://github.com/johng/stackweave/blob/main/docs/api-reference.md)

| Dir | Contents |
| --- | --- |
| `src/runloom_c/` | C extension: scheduler, channels, netpoll, asm backends, M:N hubs, stall recovery |
| `src/stackweave/` | Python layers: `aio`, `sync`, `monkey`, `time`, `runtime` |
| `tests/` · `examples/` · `benchmark/` · `docs/` | tests · runnable examples · benchmarks + perf harness · docs |

Build from source (contributors): `pip install -e .` from a clone on a patched
interpreter (needs a C compiler; `scripts/install.sh` bootstraps one). On stock
CPython, build in place with `python setup.py build_ext --inplace` and run with
`PYTHONPATH=src`, or set `STACKWEAVE_ALLOW_STOCK_CPYTHON=1` to let pip through.
