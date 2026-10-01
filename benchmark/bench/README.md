# stackweave performance-benchmark campaign

A reproducible, statistically-honest measurement layer for stackweave, plus the
profiling drivers used to explain the numbers.  This is **measurement
infrastructure**, not optimization work: the goal is that any number is
diffable, has its full environment recorded, and a regression is visible.

Primary target runtime is free-threaded CPython **3.14t** (stackweave's M:N hub
pool only gets real core-level parallelism with the GIL off, and stackweave
builds on nothing else).  asyncio and the Go loadgen are comparison baselines.

## Why not just the existing `bench/bench_*.py`?

Those print one wall-clock number from one run -- no warmup, no repetition,
no dispersion, no environment record.  Useful as a smoke check, useless as a
measurement you can compare across days or use as a regression gate.

## Layout

| Path | What |
| --- | --- |
| `harness.py` | env capture + CPU pinning + warmup/samples + median/MAD/min + bootstrap-CI median + JSON writer |
| `micro.py` | single-hub scheduler microbenchmarks (spawn, yield, chan ping-pong, buffered chan) |
| `mn.py` | M:N CPU-bound core-scaling (1..N hubs on 3.14t) |
| `mnsched.py` | M:N scheduler under migration: park/wake routing (local wake, pinned, cross-hub, busy and drifted pools), spawn / yield / pairs / fan-out / Mutex / WaitGroup / select / `blocking()`, 64-pair hub scaling, and latency percentiles (foreign-thread and cross-hub wakes, spawn-to-first-run, timer lateness) |
| `echo.py` | in-process TCP echo round-trips (all-C and Python handlers, `TCPConn` clients) at 2/4/8 hubs -- measures whichever I/O path the environment selects |
| `features.py` | feature matrix: runs suites once per opt-in switch (`STACK_ARENA`, `optimize("throughput")`, and on Linux `TCPCONN_IOURING` / the io_uring loop +/- multishot) and per build (`--build NAME=SRC`), in interleaved passes, with a delta-vs-default summary |
| `results/*.json` | committed baselines; the regression gate diffs against these |
| `profile/` | profiling drivers (cProfile, perf stat/record, perf c2c, bpftrace, memory) |
| `../scripts/bench.sh` | one-shot driver: cleanest-env run of the whole suite + report |

## Methodology

- **Build**: production-representative `-O2` (the default), plus `-g` so
  `perf --call-graph dwarf` gets accurate stacks.  Never ASan/`-O0` for a
  perf number.
- **Pinning**: this is a 64-vCPU / 2-NUMA-node VM *shared with a desktop
  session*.  We pin to a contiguous CPU set on **one NUMA node** (default
  node1, cpus 32+) to dodge cross-NUMA latency and OS/desktop preemption on
  the low cpus.  Frequency governor + turbo are not exposed (virtualized),
  so variance control is affinity + ASLR-off + statistics, not pstate.
- **Stats**: median + MAD + min(best) + %RSD + bootstrap 95% CI of the
  median.  Median/MAD are robust to the occasional preemption spike a mean
  would smear; `min_s` is the cleanest lower bound.
- **GC**: collected untimed before each sample, frozen during it.

## Run

```sh
# whole suite, cleanest env (ASLR off, pinned, one NUMA node)
scripts/bench.sh

# a single suite by hand
PYTHONPATH=src ~/.pyenv/versions/3.14.4t/bin/python -m bench.micro

# the M:N suites need the patched interpreter (migration is the only M:N mode)
PY=~/.pyenv/versions/3.14.4t-mig/bin/python3.14t
PYTHONPATH=src:benchmark PYTHON_GIL=0 $PY -m bench.mnsched      # --quick for a smoke run
PYTHONPATH=src:benchmark PYTHON_GIL=0 $PY -m bench.echo --hubs 2,4,8

# every opt-in feature vs default, two interleaved passes (A B C / C B A);
# a second build (e.g. -O3) is just another src/ tree
PYTHONPATH=src:benchmark PYTHON_GIL=0 $PY -m bench.features --passes 2 \
    --build O2=src --build O3=/path/to/O3-tree/src
```

`mnsched` and `echo` check that every fiber did its work (completion counts,
checksums, echoed bytes), so a silently failing fiber cannot pass as a fast
one.  Result files record the optimisation state that matters for a number:
the interpreter's configure args (PGO/LTO), whether TLBC is on, every
`STACKWEAVE_*` switch in force, and a free-text `STACKWEAVE_BENCH_BUILD`
label for the extension build.  Latency distributions go under `"latency"` in
the JSON, not `"results"`, so `regress.py` never gates a tail as a throughput.
Feature-matrix runs land in `results/features/<stamp>/` (`summary.md` +
one JSON and log per suite/build/config/pass).

## Campaign phases

0. **Foundation** -- worktree off origin/main, `-O2 -g` build, harness. ✅
1. **Common tools** -- harness micro/macro suites, pytest-benchmark, cProfile
   hotspots, `/usr/bin/time` + `perf stat` macro counters.
2. **Research-grade** -- `perf record/report` HW counters (IPC, cache/branch
   miss) + flame graphs, `perf c2c` false-sharing on the lock-free
   structures, `bpftrace` latency histograms (park->wake, runqueue, futex),
   memory (stack HWM distribution, RSS, tracemalloc).
3. **Reporting + regression gate** -- dated reports, baseline diffing.
