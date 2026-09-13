# stackweave — project guidance

Full derivations for the invariants below: [docs/dev/RUNTIME_GOTCHAS.md](docs/dev/RUNTIME_GOTCHAS.md).

## Build & test
- Target **free-threaded CPython 3.14t** (M:N is only real with the GIL off):
  `~/.pyenv/versions/3.14.4t/bin/python3.14`, `PYTHON_GIL=0`. (Default as of 2026-07-07:
  3.14 carries the gh-116738 stdlib-C-module free-threading audit — e.g. heapq
  now holds the list critical section, fixing a 3.13t SIGSEGV on concurrent
  shared-heap access that is NOT a stackweave bug. 3.13t builds remain available
  for p488 reproduction.)
- Build `python setup.py build_ext --inplace`; run with `PYTHONPATH=src`.
- `pip install` / `pip install -e` refuse any interpreter without both migration
  patches (setup.py install gate), so they refuse the stock 3.14t above.
  `build_ext --inplace` is ungated; set
  `STACKWEAVE_ALLOW_STOCK_CPYTHON=1` to let pip through. Guard: `tests/test_install_gate.py`.
- Run the suite via `tests/run_isolated.py` (one file/subprocess — in-process
  `pytest tests/` flakes on cross-file state leaks).

## Gating
- **Locally: `scripts/check_all_fast.sh` before any merge**;
  `scripts/check_all_extensive.sh` for a risky/large merge. The local gate is
  still the authority -- it runs everything, including the phases that need
  Spin/CBMC/TLC installed.
- **Hosted CI (`.github/workflows/ci.yml`) runs a deliberately CHEAP subset**
  on push/PR, and the full `check_all_fast.sh` on a weekly schedule. It is a
  fast smoke gate, not a replacement for the local one -- a green CI does not
  mean `check_all_fast` passes.

## Agent-shell gotchas
- Each shell caps `RLIMIT_NOFILE` at 4096 — raise it in the SAME block before a
  socket bench: `sudo -n prlimit --pid $$ --nofile=8388608:8388608` (doesn't
  persist). Kernel ceilings via `sudo -n sysctl -w …` (`vm.max_map_count`, ~2
  VMAs/goroutine, bites first at N≫100K).
- Shells run `set -e` — prefix multi-step blocks with `set +e` so a nonzero step
  (e.g. `pkill` with no match) doesn't abort the block.
- Deletions: `safe-rm`, never `rm`.

## macOS test box
- **NEVER shut it down — RESTART only.** The provider's support explicitly says
  so; a shutdown may not come back up without their intervention. So no
  `shutdown`, no `halt`, no `poweroff`, and no "stop instance" in the panel —
  `sudo reboot` if it genuinely needs a bounce.
- Credentials are deliberately NOT recorded here: this file is committed, and a
  host/password in git is a credential leak that outlives whoever needed it.
  Keep them in the operator's password manager.
- It is small on purpose (4 cores / 3 GB), which makes it a good proxy for the
  macOS CI runner — the open bugs are load-sensitive and do not reproduce on a
  big idle box. Build with `-j2`; `-j4` risks OOM at 3 GB.

## Benching
- Measure the runtime, not setup: parallel `SO_REUSEPORT` acceptors, establish
  all N first, count round-trips over a fixed window. Race-free counters (one
  `bytearray(N)` slot per goroutine — a shared `+= 1` loses increments GIL-off).
- Loopback packets traverse the host nft ruleset (~14% throughput tax) — pass
  `--netns` to big_100 for a clean number.

## Scheduler invariants
- **A freed `runloom_g` struct never returns to the OS.** `slab_free` retains it
  (refcount 0, magic DEAD); a stale dup-wake reaches `hub_submit`, which reads
  `g->refcount` via `try_incref` — only sound while the struct is a valid g.
  Freeing → garbage refcount → SIGSEGV (arm64). Guard: `tools/verify/cbmc/sched_qref_cbmc.c`.
- **Signals deliver INTO the parked goroutine, not via the scheduler.** A handler
  raising during a cooperative wait propagates out of *that call*; the idle
  scheduler carries one out of `run()` only when nothing is parked. Path:
  `runloom_netpoll_signal_wake` + the `RUNLOOM_NETPOLL_SIGNALED` sentinel.
- **Future-completion wakes are call_soon-FIFO.** `wake_safe` keeps its
  same-thread fast-path (ready-ring push), detected by PEEKing `runloom_tls_sched`
  — never `runloom_sched_get()` (mallocs on a foreign waker). Guard:
  `tests/test_differential_asyncio.py` (sc_call_soon_fifo).
- **Preemption never yields mid object-destruction.** Both yield sites gate on
  `runloom_tstate_in_destruction` and defer (trigger stays armed); yielding inside
  a `tp_dealloc` freezes a half-dead object across a GC-safe point → UAF. Don't
  reroute via the eval-breaker.
- **Cooperative primitives are foreign-OS-thread-safe.** A non-goroutine thread
  (a patched `Lock` in an mp.Queue `_feed` thread) must detect no-goroutine (TLS
  peek NULL) and block on the real OS — never park a non-existent g, never lazily
  alloc sched state (`peek_current`, never `sched_get`).
- **Parked-fiber frames are made GC-visible by the frames anchor.** The
  free-threaded collector credits PEP-703 deferred stackrefs (code objects,
  functions, deferred locals) only on LIVE tstate `current_frame` chains; a parked
  fiber's frames live in `g->snap`, invisible — so with the specializing
  interpreter on (TLBC), their deferred-only referents were freed early → resume
  UAF (the p565/p524 crash). `module_gcframes.c.inc` registers ONE GC-tracked
  anchor whose `tp_traverse` (stop-the-world only) walks the fiber registry + the
  base-snap registry and visits every parked chain (greenlet-PR#511 visit set,
  transcribed in `runloom_iframe.c`). Consequences that are now memory-safety
  load-bearing: (1) the fiber registry (`runloom_greg`) must reach the anchor —
  `STACKWEAVE_GREG_OFF` loses (the anchor refuses to activate blind), and ANY new
  spawn path that bypasses `runloom_greg_link` reopens the blind spot; (2) the
  single-thread drain's caller frames must stay registered via the base-snap
  registry (`runloom_base_snap_register`, one node per drain, paired at the single
  exit); (3) the snap seam ordering L1–L5 (frozen in `runloom_sched_pystate.c.inc`
  comments — `valid=1` last & safepoint-free on snap; `current_frame`/`c_stack_refs`
  restored before `valid=0` on load; `valid=0` last on snap_clear) must not be
  reordered; (4) the anchor is never immortal and its traverse never allocates;
  (5) `gc.freeze()` is neutralised by a gc-`start` callback that thaws the anchor.
  **TLBC stays ON iff `stackweave_c.gc_frames_active`** — `runtime.py`'s
  `_tlbc_reexec_if_needed` re-execs with `PYTHON_TLBC=0` only when the anchor is
  inactive. **greenlet coexistence on 3.14t still needs `PYTHON_TLBC=0`** (its own
  suspended-frame GC fix is 3.15-only; our anchor covers stackweave fibers, not
  greenlet's frames — see `tests/test_greenlet_interop.py`). `sys._clear_internal_caches`
  is safe during `run()` (hub tstates own their TLBC indices for the whole run),
  EXCEPT the upstream latent case of a suspended generator escaped from a since-dead
  user thread. Guard: `tests/test_tlbc_parked_frame_gc.py` (+ p565/p524 as the
  TLBC-on ground-truth oracle).

- **Offload hubs must stay invisible to general work.** `STACKWEAVE_OFFLOAD_HUBS=K`
  (default 0 = off) reserves K hubs at the **tail** of `runloom_hubs[]` to run
  blocking calls as ordinary fibers, so `offload` can reuse the scheduler
  instead of the bespoke thread pool + self-pipe + result-box in
  `monkey/_base.py`. It needs no CPython tstate patch **because nothing
  migrates**: the offload fiber is born and dies on its hub, and the caller
  parks on a normal channel on its own hub. The whole design rests on one
  invariant — *no general work ever lands on an offload hub*, or it strands
  there exactly as it would on any blocked hub. **Four** exclusions enforce it
  and all must hold together (`runloom_general_hub_count()` bounds each; it
  equals `runloom_hub_count` when off, so every path is unchanged by default):
  (1) spawn placement in `runloom_mn_fiber_core`; (2) steal rotation in
  `hub_main` — an offload hub never steals, and general hubs never take it as a
  victim, which fail differently; (3) `sysmon` preempt dispatch — it blocks on
  purpose, and CPU-bound offloads run ATTACHED so the `tss` test alone would not
  spare them; (4) **`world_yield_if_monopolizing`** — it arms on DETACHED with
  `pending>0`, which is an offload hub's *steady state*, so leaving it in the
  scan pauses every general hub 100µs on a loop for the duration of every
  offload (a slowdown, not a failure — the easiest one to reintroduce).
  `any_stealable_work` / `wakep_one` are bounded for the same reason. New spawn
  paths must route through `runloom_mn_fiber_core(..., force_hub)`, never a
  second path, or the parked-frame GC blind spot below reopens. The ONE
  sanctioned breach is `mn_fiber(hub=N)`, bounded by `runloom_hub_count` not
  `runloom_general_hub_count()`, so a test can force general work onto an
  offload hub and assert the exclusions from the inside. Liveness only — pinned
  to a BUSY offload hub the fiber strands; nothing migrates, so never a
  soundness hazard. Guard: `tests/test_offload_hubs.py`.

## aio bridge invariants (src/stackweave/aio/)
- Layout: `_base.py` is the foundation (`_go_io`, `_wait_fd`, `_CURRENT_TASKS`);
  the loop is composed from `loop_*.py` mixins; internals reachable via PEP 562
  `__getattr__`.
- **Protocol-callback goroutines need a roomy stack.** `data_received` /
  `connection_made` / … run user C-recursing code (asyncssh kex) → guard-page
  SEGV on a grown-down stack. Spawn via `_go_io` (`_IO_STACK`, 512 KB); don't
  revert to bare `stackweave_c.fiber`.
- **Timer goroutines read the callback THROUGH the handle.** Capturing
  `callback`/`args` in the runner closure leaks cancelled timers' graphs. Guard:
  `tests/test_swarm_aio_bridge.py::test_cancelled_call_later_does_not_leak_callback_graph`.
- **`_StreamTransport` seeds `self._io_g = None` before `connection_made`.** A
  write inside connection_made kicks io before `__init__` finishes; the post-cm
  spawn is `if self._io_g is None`. Guard: `tests/test_adv_aio.py::test_connection_made_write_reaches_client`.
- **Loop-level callbacks run with no current task.** Route via `_pg_run_loop_cb`
  (clears `_CURRENT_TASKS[loop]`), else a stock-Task wakeup hits enter_task and
  the wake is dropped. Guard: `tests/test_swarm_aio_bridge.py::test_loop_level_callback_has_no_current_task`.
- **`Server.close()` wakes its accept loops** — `cancel_wait_fd()` the parked
  accept goroutines or they leak. Guard: `tests/test_adv_aio.py::test_server_close_does_not_leak_accept_fibers`.
- **`loop.sock_*` releases the fd's netpoll arm on completion.**
  `@_release_fd_after` → `netpoll_release_if_idle(fd)`, else a reused fd number
  hangs on the stale arm cache. Don't drop the decorator or the register-once
  skip. Guard: `tests/test_aio_fd_reuse.py`.
- **Future done-callbacks defer through call_soon, in asyncio order.**
  `_fire_callbacks` defers all but `StackweaveTask._wake_unpark` and
  `_runloom_fire_sync`-tagged callbacks. Guard: `tests/test_differential_asyncio.py` (sc_done_callback_order).
- **The driver resumes with `coro.send(None)`, never `send(future.result())`.** A
  custom awaitable-iterator takes the `.send()` branch on a non-None value and
  raises. Guard: `tests/test_differential_asyncio.py` (sc_send_none_protocol).

