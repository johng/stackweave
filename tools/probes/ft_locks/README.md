# Probes: CPython locks and thread identity under fibers

Findings: `docs/dev/FT_CPYTHON_LOCK_GAPS.md` (numbers in brackets).
Run: `./run.sh pNN_name.py [args]` (uses this worktree's `src/` and
`~/.pyenv/versions/3.14.4t-mig`, override with `PY=`). Each prints PASS or
FAIL, or hangs into its watchdog (rc 3).

| probe | args | finding |
|---|---|---|
| p01_gilstate.py | `sqlite\|ctypes [H]` | PyGILState from a fiber works (fine) |
| p02t_buffered_threads.py | (plain python) | [16] upstream readline data loss |
| p03_blocked_hub_netpoll.py | `H secs` | blocked hub doesn't stall others' fd wakes (fine) |
| p04_buffered_reentrant.py | `park-read\|preempt-read\|preempt-write H same\|diff` | [8] false reentrant call |
| p05_preempt_pin_vs_hub.py | `mn\|mn_pin\|run_hub\|run_pin` | [14] pinned fibers don't time-slice |
| p06_pin_starve.py | | [14] pinned fiber waits ~64 slices |
| p07_del_parks.py | `sleep\|chan H N` | [10] finalizers run with no fiber |
| p08_mainthread.py | `[patch]` | [18] main-thread APIs |
| p09_preempt_holding_lock.py | `stock\|rlock\|patched\|strptime\|strptime-patched` | [6] preempted holder deadlock |
| p10_sqlite_udf_migrate.py | `park\|migrate [serialized]` | [1] sqlite callback + second fiber |
| p11_sqlite_udf_min.py | `multithread\|serialized` | [1] sqlite callback migrate abort |
| p12_brc_wrong_thread_merge.py | `H rounds` | [2] wrong-thread refcount merge (fixed) |
| p13_sqlite_busy.py | `H` (H>=2) | [7] busy handler blocks the hub |
| p14_mp_semlock.py | `[patch]` | [5] mp.Value lock self-deadlock |
| p15_osread_nonblock.py | | [13] fd left non-blocking |
| p16_warnings_lock.py | `H [quiet]`, env `WMODE=showwarning\|stat` | [3] warnings lock leak |
| p17_import_lock_finder.py | | [4] import lock leak |
| p18_setasyncexc.py | | [9] SetAsyncExc hits a bystander |
| p19_fork_signal.py | `plain\|thread\|mn` | [17] fork child signal latency |
| p20_thread_daemon.py | `flagfile` | [12] fiber-started thread is daemon |
| p21_tlbc_per_fiber.py | `N` (try `PYTHON_TLBC=0`) | [15] per-fiber TLBC copies |
| p22_asyncio_debug.py | | [11] asyncio debug thread check |
