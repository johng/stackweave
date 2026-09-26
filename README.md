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

## Stackweave vs Go

Same box (64c, free-threaded CPython 3.13t), 8 hubs / `GOMAXPROCS=8`, warm
steady-state. Go ≈ 2.1 M spawn/s here.

| metric | stackweave | Go | verdict |
| --- | ---: | ---: | --- |
| **spawn** — pure C (`c_entry`) | **2.29 M/s** | 2.10 M/s | **beats Go** |
| **spawn** — Python (`stackweave.fiber`) | 1.35 M/s | 2.10 M/s | 0.65× |
| **context switch** | ~75 ns yield · ~560 ns chan RT | ~50 ns `Gosched` | ~parity |
| **conn/s** — churn (new conn per req) | ~75–78 k/s | ~75–78 k/s | **parity** |
| **req/s** — keep-alive echo, Python handler | 596 k/s | 603 k/s | **0.99× — parity** (C handler beats Go) |
| **memory** — empty parked fiber | 8.8 KB | 2.7 KB | 3.3× (the one real gap) |

The short story: on **spawn, scheduling, and throughput, stackweave trades blows
with Go and beats it on raw spawn** — a stackful coroutine runtime on CPython
matching a compiled language even with a Python handler (596 k vs 603 k req/s at
saturation; a C handler beats Go). The one honest gap left is **memory**: a
suspended fiber carries a CPython eval frame, ~3.3× Go's per-fiber RSS.
Full cross-runtime numbers + cold spawn-vs-N curves: **[benchmark report](https://github.com/johng/stackweave/blob/main/benchmark/report.html)**
· [perf summary](https://github.com/johng/stackweave/blob/main/docs/dev/PERF_SUMMARY.md).

```python
stackweave.optimize("throughput")   # stackweave.fiber -> max spawn rate (fiber_fast)
stackweave.optimize("memory")       # stackweave.fiber -> small right-sized stacks (default)
```

## Install

stackweave installs only onto a free-threaded CPython built with its migration
patches — see [src/patches/](src/patches/README.md) (`tools/ci/build_patched_cpython.sh 314`
builds one). `pip install` refuses a stock interpreter. Install with the patched
interpreter's pip:

```bash
/path/to/patched/bin/python3.14 -m pip install stackweave
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
  submission, woken goroutines routed back to their origin hub.
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
