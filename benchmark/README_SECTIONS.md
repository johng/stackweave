## Benchmarks

Measured on Intel Xeon CPU E5-2696 v3 @ 2.30GHz, 64 vCPU, 78.6 GiB (free-threaded CPython 3.13t, GIL off) under a two-netns veth topology with disjoint CPU pinning. Full tables, per-connection curves, assumed constraints and every benchmark program are in the detailed report.[^bench]

### Echo throughput (1 KiB requests)

Requests/second, shown raw (as measured); the cores column is the core count behind each number, not divided out.[^bench]

| Server | Cores | req/s | CPU-bound side |
|---|--:|--:|---|
| Stackweave io_uring + Cython C handler | 44 | 638,516 | client |
| Stackweave io_uring + Cython + optimize(throughput) | 44 | 637,619 | client |
| Stackweave io_uring + Cython cdef handler (tstate-free c_entry) | 44 | 634,343 | client |
| Stackweave epoll + Cython cdef handler (tstate-free c_entry) | 44 | 631,569 | client |
| Stackweave C scaffold + Cython C handler (epoll) | 44 | 630,502 | client |
| Stackweave C scaffold (py handler, C TCPConn) | 44 | 624,290 | client |
| Go net (GOMAXPROCS=44) | 44 | 602,672 | client |
| Stackweave sync wrappers (epoll, py handler) | 44 | 595,577 | client |
| Stackweave io_uring loop (py handler) | 44 | 592,376 | client |
| uvloop (GIL, 1 core) | 1 | 57,768 | server |
| asyncio Protocol (GIL, 1 core) | 1 | 44,323 | server |
| gevent StreamServer (GIL, 1 core) | 1 | 22,685 | server |

> The 16-core Go loadgen saturates before the fastest servers (`client`-bound rows); the report gives a server-ceiling estimate from server CPU utilisation.[^bench]

> **io_uring:** driven through the Stage-2 proactor (`loop_recv`), the io_uring loop backend is a major win &mdash; the Cython handler on io_uring reaches a **1.16M req/s server ceiling (+2.17× over epoll)**, the fastest stackweave config measured. "io_uring loses on loopback" was an artifact of driving it through the readiness path; see the findings writeup.[^bench]

### Handler work curve (what compiling the handler buys)

Echo ties every handler optimisation because it does no CPU work in the handler. This is the one experiment that gives the handler something to do: **one server, one knob** (`--work N` = an FNV-1a byte hash over the 1024 B payload, repeated N times), **two builds of the identical algorithm** &mdash; interpreted Python vs Cython-compiled &mdash; on the same runtime and I/O path. `--work 0` **is** the echo (lowest point), so it consolidates the echo load and reproduces it as a cross-check.[^bench]

| --work (FNV passes) | Python handler req/s | Cython handler req/s | Cython / Python |
|--:|--:|--:|--:|
| 0 (echo) | 615,882 | 609,964 | 0.99× |
| 1 | 84,989 | 616,838 | 7.26× |
| 4 | 24,788 | 625,008 | 25.21× |
| 16 | 6,829 | 584,877 | 85.64× |
| 64 | 1,750 | 287,390 | 164.18× |

> As the knob grows the interpreted handler goes server-bound and collapses while the compiled handler holds (up to **164.2×** here). The work is pure inline arithmetic, never offloaded to a worker thread. **Honest framing:** if the handler delegated to a C library (`hashlib`/`json`/`struct`) Python and Cython would converge &mdash; the gap is specific to *handler-level* Python work.[^bench]

### Real-work handler curve across runtimes (raw throughput)

The same `--work` FNV hash in every runtime's natural handler language, reported as raw peak req/s (the cores column shows the core count behind each number, not divided out). It shows the result is honest: under real CPU work the **handler language** sets the tier, not the runtime.[^bench]

| Runtime | handler | cores | req/s @ echo | req/s @ work=64 |
|---|:--|--:|--:|--:|
| Go net (GOMAXPROCS=44) | compiled | 44 | 594,958 | 308,452 |
| Stackweave (M:N) — Cython handler (compiled) | compiled | 44 | 610,351 | 287,416 |
| Stackweave (M:N) — Python handler | interpreted | 44 | 619,914 | 1,716 |
| asyncio Protocol (1 core) | interpreted | 1 | 38,122 | 31 |
| uvloop (1 core) | interpreted | 1 | 53,073 | 31 |
| gevent StreamServer (1 core) | interpreted | 1 | 20,737 | 29 |

> Two bands by handler language: the compiled handlers (runloom-Cython, Go, both on the full core set) sit together under load, the interpreted ones (runloom-py, asyncio, uvloop, gevent) sit together below. Cores differ — stackweave and Go use the whole machine, the event loops one core (the cores column makes that explicit, so compare within a matched core count). stackweave's edge: it reaches the compiled band while keeping M:N across all cores automatically; one asyncio process serialises the same work onto one core. Delegate to a C lib and all runtimes re-converge.[^bench]

### Memory per idle fiber

Used resident memory (RSS, not virtual) for 1,000,000 live parked fibers/goroutines.[^bench]

| Config | total RSS | bytes / fiber |
|---|--:|--:|
| go | 2.47 GiB | 2,652 |
| stackweave_c | 4.50 GiB | 4,833 |
| runloom_py_optmem | 8.24 GiB | 8,844 |
| runloom_py | 8.24 GiB | 8,845 |

### Scheduler micro-benchmarks

| Runtime | spawn (tasks/s) | ctx-switch (ns) |
|---|--:|--:|
| stackweave | 1,910,000 | 23,715 |
| go | 2,240,000 | 708 |
| asyncio | 78,847 | 1,881 |
| uvloop | 94,605 | 932 |
| greenlet | 53,869 | 447 |

> Stackweave fibers carry real C stacks (heavier to spawn than goroutines); its loaded-yield context-switch hits the free-threaded refcount wall at high hub counts. Strength is parallel I/O throughput, not single-stream latency.[^bench]

[^bench]: Full data, methodology, per-connection ladder curves, the assumed constraints, every benchmark program's source, and the zero-PyObject Cython disassembly proof: [`benchmark/report.html`](report.html). Cross-platform backend syscall profiles (Linux/macOS) are linked from there.
