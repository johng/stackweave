# stackweave benchmarks

One set of workloads, run on stackweave (per feature config) and on the
runtimes people would otherwise use: OS threads, asyncio, uvloop, trio,
gevent and Go.  Four suites: scheduler microbenchmarks (`mnsched`), TCP echo
(`echo`), application-shaped programs (`apps`) and memory per parked unit
(`memory`). Every runtime runs the same entries, with the same names and
inner counts, so one table compares them all.

| Path | What |
| --- | --- |
| `mnsched.py` | stackweave scheduler workloads on M:N hubs (migration is the only M:N mode): park/wake routing (local wake, pinned, cross-hub, busy and drifted pools), spawn / yield / pairs / fan-out / Mutex / WaitGroup / select / `blocking()`, 64-pair hub scaling, latency percentiles (foreign-thread and cross-hub wakes, spawn-to-first-run, timer lateness) |
| `echo.py` | stackweave in-process TCP echo round-trips (all-C and Python handlers, `TCPConn` clients) at 2/4/8/16 hubs -- measures whichever I/O path the environment selects |
| `apps.py` | application-shaped programs on stackweave: an HTTP/1.1 JSON API (64 keep-alive clients), an API gateway fanning out to 8 slow backends per request, a CPU-bound parse+hash pipeline, a 256-subscriber pub/sub broadcast, a 10k-task crawler |
| `memory.py` | RSS growth per parked fiber at 10k and 100k, from the OS |
| `appspec.py` | the one definition of the apps and memory workloads (sizes, payloads, HTTP framing, checksums) that every Python runtime imports and Go mirrors |
| `baselines.py`, `baselines_apps.py` | the same entries on threads, asyncio, uvloop, trio and gevent (no stackweave imported; run on a stock interpreter) |
| `gobench/` | the same entries in Go (GOMAXPROCS stands in for the hub count) |
| `compare.py` | the driver: every stackweave config and every other runtime as a column, interleaved passes, one `summary.md` |
| `harness.py`, `gil.py` | env capture + warmup/samples + median/MAD/min + bootstrap CI + JSON writer; the free-threading guard |
| `regress.py` | min_s regression gate between two runs |
| `results/compare/<stamp>/` | committed runs: `summary.md` + one JSON and log per suite/column/pass |
| `../../scripts/bench.sh` | one-shot driver around `compare.py`, with an optional gate against an earlier run |

## Run

```sh
PY=~/.pyenv/versions/3.14.4t-mig/bin/python3.14t      # patched 3.14t (migration needs it)
# a stock free-threaded interpreter with the event loops, for the baselines
~/.pyenv/versions/3.14.4t/bin/python3.14 -m venv /tmp/swbase
/tmp/swbase/bin/pip install uvloop trio gevent

# everything: every stackweave config + every runtime, 2 interleaved passes
STACKWEAVE_BASELINE_PYTHON=/tmp/swbase/bin/python \
PYTHONPATH=src:benchmark PYTHON_GIL=0 $PY -m bench.compare
# a GIL build adds asyncio's / uvloop's single-threaded best case
... --gil-python /opt/homebrew/bin/python3.14 --runtimes threads,asyncio,asyncio-gil,go
# a subset, a quick smoke run, two extension builds A/B
... --configs default,stack-arena --runtimes go --passes 3 --quick
... --build O2=src --build O3=/path/to/O3-tree/src --runtimes ''

# one suite / runtime by hand
PYTHONPATH=src:benchmark PYTHON_GIL=0 $PY -m bench.mnsched      # --quick, --only NAME
PYTHONPATH=src:benchmark PYTHON_GIL=0 $PY -m bench.echo --hubs 2,4,8
PYTHONPATH=benchmark /tmp/swbase/bin/python -m bench.baselines --kind trio
(cd benchmark/bench/gobench && go run . -quick)
```

Configs whose feature the box cannot run (the io_uring ones off Linux) and
runtimes it cannot run (an event loop not installed in the baseline
interpreter, no `go`) are skipped with a note.

## Reading the table

- A cell is the median over passes. Throughput rows are ops/s (higher is
  better); `[p50]` / `[p99]` rows are latency and `[RSS/unit]` rows are
  resident memory per parked unit (lower is better).
- The bracket compares the cell with stackweave `default`: a percentage for
  another stackweave config, a ratio (value / stackweave's) for another
  runtime. ▲ / ▼ mark better / worse only when every pass agrees and the gap
  is over 3%.
- `-` means the runtime has no analogue: `select` has none in the Python
  stdlib, the pinned / busy / drifted / `@Nh` rows are stackweave
  scheduling variants (Go runs the `@Nh` rows with GOMAXPROCS=N), and Go has
  no foreign OS thread without cgo.
- Echo: the event loops and threads have no hub count, so their number sits
  in the default-hubs row (`py-echo @4h`); Go's handler is native code, so its
  number fills both the `c-echo` and `py-echo` rows of each hub count.
- Caveats that move a column, documented in `baselines.py` / `gobench/main.go`:
  asyncio's Lock and gevent's Semaphore never contend (nothing yields inside
  the critical section) while trio's does; threads call the `blocking()` row's
  sleep directly, the loops go through their thread offload; Go's
  `blocking()` is `time.Sleep`, a runtime timer.
- Apps are written the way each runtime is normally used: Go uses net/http
  (a full HTTP stack) where the Python runtimes share one small HTTP/1.1
  parser; threads run a thread per connection / task and a
  ThreadPoolExecutor for the gateway's backend calls. The single-threaded
  loops run the pipeline's CPU work on one core by design -- that is the
  point of that row. Threads skip the 100k memory row.
- Memory is the OS's RSS growth while N units are parked, so it includes
  stacks, thread states and allocator overhead -- and the page size (16 KB on
  Apple silicon, 4 KB on x86 Linux) rounds it up.

## Methodology

- **Correctness first**: every entry checks its work (completion counts,
  checksums, echoed bytes), so a silently failing unit cannot pass as a fast
  one.
- **Isolation**: every stackweave `mnsched` entry runs in its own fresh
  interpreter (grow-down sizer state outlives `mn_fini`); every column runs in
  its own process, and columns are interleaved per pass (A B C, then C B A).
- **Stats**: median + MAD + min + %RSD + bootstrap 95% CI of the median; GC
  collected untimed before each sample and frozen during it.
- **Provenance**: each JSON records the interpreter's configure args (PGO /
  LTO), TLBC, every `STACKWEAVE_*` switch, the load average, the P/E core
  layout, and `STACKWEAVE_BENCH_BUILD` (a free-text extension build label).
- **Noise**: compare runs from the same day on the same box. Cross-day deltas
  on a laptop or shared VM are mostly noise (±5% is normal). On macOS nothing
  pins hub threads; a run that lands hubs on efficiency cores reads ~2.4x
  slower on ping-pong, so check the load before trusting a gap.

What this does not measure yet: an external load generator across network
namespaces, connection churn, and spawn rate as N grows to 1M. The old
`suite/` measured those on Linux; it was retired with this consolidation (see
git history) and they are the next things to add here.
