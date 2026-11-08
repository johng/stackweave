# stackweave — project guidance

The invariants below are recorded here in full; [docs/dev/](docs/dev/) holds other design notes.

## Build & test
- **Migration-only tree.** Cross-hub fiber migration is always on. The env
  vars that chose the old per-hub-tstate scheduler or only turned a default off
  are gone (numeric knobs and debug/fault/sim hooks remain). A few opt-in
  features keep their env var: `STACKWEAVE_TCPCONN_IOURING`,
  `STACKWEAVE_IOURING_LOOP` (+ `IOURING_MS`) and `STACKWEAVE_STACK_ARENA*` work
  under migration. The bulk `fiber_n` spawn path (`STACKWEAVE_GON_*`) was
  removed: it duplicated the spawn path's invariants for no day-to-day use.
- **Broken features are kept, not deleted.** A feature that does not work under
  migration keeps its code and env var, is guarded off with a one-time stderr
  note, and has strict xfails that assert it actually ran, after the test's own
  correctness checks. Known gaps are xfail, never skip, and every one is marked
  at its test with a `tests/known_gaps.py` helper (`KNOWN_GAP`,
  `MIGRATION_GAP`, `INTERMITTENT`, `SEEDED_MN_TODO`; conftest rejects a bare
  xfail, and `pytest -m known_gap` lists them). Fixing one turns its xfails into XPASSes;
  then drop the xfails and keep a positive it-ran check (the loop backend's
  `assert_iouring_loop_ran` in `tests/adv_util.py` reads the hub ring-wait
  count).
- Migration is only sound on a free-threaded
  CPython built with BOTH `src/patches/` halves (alloc-home + exec-home) — on a
  stock interpreter it crashes under churn at H≥2 (mimalloc `_mi_page_retire`,
  or a dict free under `_Py_MergeZeroLocalRefcount`). Nothing in the ABI tells
  the two thread-state layouts apart, so stackweave compares the build with the
  interpreter: `import stackweave` warns when `stackweave_c.migration_patched`
  differs from what `sysconfig` says the interpreter was built with (a stale
  build from the other interpreter, or `-D` flags forced on a stock one), and
  `mn_init` warns once at H≥2 when the extension was built without both.
- **Free-threaded CPython 3.14+ only** (M:N is only real with the GIL off):
  `setup.py` refuses GIL builds and anything older, and the C sources have no
  other code paths (`#error` in `runloom_sched.h`). Every fiber stack is >= 256 KB
  there, with an overflow raising `RecursionError`, not crashing. Target the
  patched **3.14t**: `~/.pyenv/versions/3.14.4t-mig/bin/python3.14`, `PYTHON_GIL=0`
  (`3.14.4t` is stock: fine for building, crashes as above under migration).
  Build that interpreter from `src/patches/` with **no `--with-lto`** (LTO
  inlines pystate.c's thread-state reads and silently undoes exec-home); PGO
  alone is fine. `tools/ci/check_exec_home_tls.py <python>` checks a build.
- Build `STACKWEAVE_EXTRA_CFLAGS="-DPy_TSTATE_ALLOC_HOME -DPy_TSTATE_EXEC_HOME"
  python setup.py build_ext --inplace` (an interpreter whose pyconfig.h lacks
  the flags doesn't pass them on, and without them the alloc-home borrow
  compiles out); run with `PYTHONPATH=src`. Switching interpreters or flags in
  one tree recompiles everything (a stamp in `build/temp.*`); before that,
  build_ext reused the other interpreter's extension and it segfaulted.
- `pip install` / `pip install -e` refuse any interpreter without both migration
  patches (setup.py install gate), so they refuse a stock 3.14t.
  `build_ext --inplace` is ungated; set
  `STACKWEAVE_ALLOW_STOCK_CPYTHON=1` to let pip through. Guard: `tests/test_install_gate.py`.
- On a patched interpreter, build greenlet from source
  (`pip install --no-cache-dir --no-binary greenlet greenlet`). The PyPI
  wheel is built against stock headers and hangs on its first `switch()`
  there, which makes every `tests/test_greenlet_interop.py` test time out.
  Rebuild it whenever the patches change `_PyThreadStateImpl` (the `_fiber`
  field did), and bypass pip's cache: it keys a locally built wheel on the
  `cp314t` tag alone, so it reinstalls one built against the old layout,
  which hangs the same way. Hosted CI doesn't install greenlet, so that file
  skips there.
- Run the suite via `tests/run_isolated.py` (one file/subprocess — in-process
  `pytest tests/` flakes on cross-file state leaks). It and `tests/conftest.py`
  refuse to run unless migration is sound: the GIL off, and both the
  interpreter and the extension built with both patches. Nothing in the suite
  skips for want of free threading or M:N any more, so a stock run fails
  loudly instead of passing on a fraction of the tests. A child interpreter
  goes through `adv_util.run_python()` / `child_env()`, which fail the test on
  a timeout rather than skip it.

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
  (refcount 0, magic DEAD); a stale dup-wake reaches `runloom_mn_wake_g`, which
  loads and CASes `g->wake_state`, and a cross-hub introspection reader calls
  `try_incref` on it — only sound while the struct is a valid g (a completed g
  keeps `wake_state` RUNNING, so a stale wake enqueues nothing). Freeing →
  garbage state → SIGSEGV (arm64). No model covers the stale read since the
  per-hub wake path went (with `sched_qref_cbmc.c`); `big_100/p305` is the
  stress sentinel.
- **Signals deliver INTO the parked goroutine, not via the scheduler.** A handler
  raising during a cooperative wait propagates out of *that call*; the idle
  scheduler carries one out of `run()` only when nothing is parked. Path:
  `runloom_netpoll_signal_wake` + the `RUNLOOM_NETPOLL_SIGNALED` sentinel.
- **In a child forked in a fiber, the fiber's state is CPython's main one,
  and `_PyRuntime.main_tstate` never names a freed state.** CPython never
  moves `main_tstate` after fork, and a signal sets its pending bit there; a
  child forked from a fiber's per-g state on a hub thread had it naming the
  old main thread's deleted state, so a bytecode loop ran handlers only at
  the next EINTR. A `pthread_atfork` child handler (`runloom_mn_atfork_child`,
  inside `fork()`, before CPython's after-fork code deletes the other states)
  makes the forking fiber's state main and takes a ref on its g that the child
  never drops, so stackweave never clears that state there. It also points the
  state's alloc home back at itself and stamps its (empty) heaps with this
  thread's id (`runloom_iframe_fork_child_own_heap`): the fiber borrowed its
  hub's allocator, and the after-fork code deletes the hub's state, so the
  child allocated through freed memory (Linux 3.14 segfaulted in
  threading's own after-fork hook). Any other fork
  puts back the embedded initial state (which a fork resets, never frees) if
  `main_tstate` names a state the fork is about to free: a grandchild forked
  from a plain thread would otherwise keep naming the freed fiber state, and
  every signal would write into freed memory. Never point `main_tstate` at a
  state stackweave or a fork may free, and never set it later than the atfork
  handler. A fork from a plain non-main thread keeps upstream's EINTR-only
  handling. Guards: `tests/test_cpython_thread_keyed_locks.py -k
  "forked_from_a_fiber_runs_signal or never_name_a_freed"`
  (`stackweave_c._main_thread_state()`, `_alloc_home_is_own()`),
  `tests/test_import_lock.py::test_a_child_forked_from_a_fiber_can_import`.
- **Only a hub's own thread edits its sleep heap.** A signal-woken io sleeper
  (`sched_sleep_io`: CoPoll, no-fd `select`) resumes through the global
  run-queue on any hub, so it never removes its own entry: the entry is
  abandoned, and the hub it parked on drops it (`runloom_sched_sleep_purge` via
  the `sleep_purge` mailbox, or the pop at the deadline). Three things make an
  abandoned entry harmless, and all must hold: entries are by value (the heap
  never reads `g->wake_at`); each carries its sleep's `g->sleep_ticket`, a
  per-sleep generation, so it cannot claim the fiber's next sleep; and on a hub
  heap each holds a g ref, so it never points at a recycled g. Guard:
  `STACKWEAVE_DEBUG=sleepheap` (aborts on an off-owner edit) +
  `tests/test_cross_hub_migration.py -k sleep`.
- **Future-completion wakes are call_soon-FIFO.** `wake_safe` keeps its
  same-thread fast-path (ready-ring push), detected by PEEKing the caller's own
  scheduler with `runloom_sched_peek_here` (`runloom_tls_sched`, or an M:N
  fiber's own) — never `runloom_sched_get()` / `_get_here()` (they malloc a
  scheduler on a foreign waker). Guard: `tests/test_differential_asyncio.py`
  (sc_call_soon_fifo).
- **A thread state runs code only under C-stack limits for the stack it is
  on.** `runloom_coro_rearm_stackprot` arms the attached state's limits at a
  fiber's own coroutine stack on every resume. 3.14's overflow check
  (RecursionError) and `_Py_Dealloc`'s trashcan margin both measure the stack
  pointer against them. On any other stack the check never raises, so deep
  recursion runs off the real stack (SIGSEGV), and the margin is wrong: on a
  stack above the limits it never defers, so a deep free crashes too; on one
  below them it is under 2, and each GC-tracked object is parked on the
  state's trashcan list (`delete_later`), which only a later GC-type dealloc
  with margin >= 4 on that state frees and `PyThreadState_Clear` never does --
  a leak. Two places leave a state's stack
  without detaching it, and each puts the right limits back:
  (1) M:N: `runloom_iframe_brc_release` drains a parked fiber's
  biased-refcount queue on the HUB's stack with the fiber's state attached, so
  it lends the fiber the hub's limits (set from the hub thread's stack when its
  state first attaches), and a freed per-g state has its trashcan list
  destroyed first (`runloom_iframe_drain_trashcan`) -- an undeallocated
  memoryview once pinned its array there; (2) the single-thread drain and
  `_Coro.resume` run fibers on the CALLER's own state, so they save its limits
  before each resume and restore them as soon as it returns
  (`runloom_drain_resume`, `RunloomTstateSnapshot`) -- unrestored, `run(1)`'s
  caller kept the last fiber's limits and a deep `repr()` segfaulted. New code
  that resumes a fiber on a state it keeps using, or frees objects on a hub
  stack under a fiber's state, must do the same. Guards:
  `tests/test_cross_hub_migration.py::test_cross_hub_drops_are_freed_under_the_hub_stacks_limits`
  (deterministic: `stats()["brc_release_merges_off_stack"]` must stay 0) and
  `tests/test_c_stack_limits.py` (`stackweave_c._c_stack_limits()` before and
  after must match).
- **`runloom_iframe_brc_release` drains with the FIBER heading the bucket.** A
  hub's own state is detached for the whole fiber resume, and from CPython
  3.15.0rc3 (gh-157838) a dropper that finds a DETACHED owner suspends it and
  merges its queue itself -- rewriting `ob_ref_local`/`ob_tid` of objects that
  thread owns, on the promise a suspended owner can't touch them. The hub's
  thread can: the drain runs deallocators under the fiber's state, which shares
  the hub's thread id. So release() drains while the fiber (attached) still
  heads the bucket, and moves the hub to the head only once the queue is empty,
  in the same bucket-mutex hold as the emptiness check. Never put the hub at the
  head before a drain, and never run object code on a hub thread while its
  detached hub state heads the bucket. Guard:
  `tests/test_cross_hub_migration.py::test_cross_hub_drops_drain_before_the_hub_heads_the_bucket`
  (`stats()["brc_release_merges_hub_first"]` must stay 0).
  "Queued" includes the fiber's private `local_objects_to_merge`: a `__del__`
  run by the fiber's own merge loop that parks leaves the rest of that stack
  on it, and a fiber that resumed on another hub would merge it there, off the
  owner thread (`_Py_ExplicitMergeRefcount` zeroes `ob_ref_local`
  non-atomically). release() drains both stacks at the park; the suspended
  loop then finds its stack empty and returns. The rest of that batch is
  therefore deallocated on the hub with no current fiber, where a patched lock
  or sleep in a `__del__` blocks the hub's OS thread (and deadlocks it if a
  fiber on that hub holds the lock). That is the open finding #10 in
  docs/dev/FT_CPYTHON_LOCK_GAPS.md; merging on the owner thread comes first.
  Guard:
  `tests/test_cpython_thread_keyed_locks.py::test_memory_queued_objects_merge_on_their_owner_thread`
  (`stats()["brc_release_local_drains"]` must be > 0 there).
- **A run(1) drain or Coro nested on a hub's fiber is its own world.** The
  single-thread drain (`run(1)`, stackweave's asyncio loop) and `_Coro.resume`
  run other fibers on the calling fiber's stack and thread state.
  `runloom_mn_nested_enter`/`_exit` bracket both, and three things hold inside:
  (1) **The hub is hidden.** `runloom_tls_hub`/`runloom_tls_current_g` still name
  the hub and the OUTER g there, so a sleep, park or yield that asked "am I a hub
  fiber?" parked or queued the still-running outer g while only the inner
  coroutine swapped out (inner fiber lost, outer g resumed twice; the per-g
  snapshot skip also made inner fibers share one frame chain). While
  `g->nest_depth > 0` the `runloom_mn_current_*` / `runloom_mn_tls_current_g`
  accessors return NULL, `runloom_mn_yield_current` returns 0, and the two
  preemption hooks this applies to -- the sysmon eval-frame wrapper
  (`runloom_preempt_eval_frame`) and the liveness pending call
  (`runloom_mn_liveness_pending_cb`) -- skip. Code that asks through those
  accessors takes its single-thread branch; new fiber-context code must do the
  same, never read the hub TLS directly. (2) **The single-thread scheduler is
  the outer g's own** (`g->nest_sched`, `runloom_g_nest_sched`), never the hub
  thread's: nested fibers are suspended on the outer g's per-g tstate (their
  snaps' exc_info chain ends in it), and a shared per-thread scheduler let
  another fiber's run(1) on that hub resume them on its tstate, or on the outer
  g's after it was freed (use-after-free). Spawn and drain entry points take it
  via `runloom_sched_get_here`; the wake paths treat it as same-thread for the
  outer g (`runloom_sched_peek_here`). The nested scheduler must not outlive the
  outer g's tstate as a runnable thing: at the outer g's end
  (`runloom_g_nest_sched_release`) everything queued, sleeping or
  in wait_fd is dropped unrun, as `sched_reset` drops it; it is then freed, or,
  if fibers parked on a channel/`park()` remain (`park(timeout=)` too: `run()`
  releases its timer entry when the drain returns; `nest_live` counts
  unfinished fibers, not live handles), reported and kept but never drained. (3) **Spawns made in a drain stay in it**
  (`runloom_mn_spawns_to_hubs`, which every spawn helper routes on instead of
  `mn_hub_count()`): a waiter there parks in the nest, which would return
  without waiting for work sent to the hubs (gather/JoinSet). Liveness cost: a
  nested run(1) holds its hub until it returns, so that hub's other fibers,
  local FIFO, sub-list, poller and ring wait meanwhile (a 10 ms sleeper there
  took ~600 ms behind a 500 ms nest), as behind any long-running fiber that
  never yields. So running a whole program nested (an aio.run in an M:N fiber)
  costs its hub for that long. Guard:
  `tests/test_nested_run1_in_mn.py`.
- **Preemption never yields mid object-destruction.** Both yield sites (the
  `preempt_init` time-slicer, and the seeded controller's frame-count hook,
  compiled only with `RUNLOOM_MN_CTRL`) gate on
  `runloom_tstate_in_destruction` and defer (trigger stays armed); yielding inside
  a `tp_dealloc` freezes a half-dead object across a GC-safe point → UAF. Don't
  reroute via the eval-breaker.
- **A thread-local's address never survives a park.** The compiler computes it
  once per function and reuses it after calls (macOS keeps the `tlv_get_addr`
  result, aarch64 Linux the `tpidr_el0` base, x86-64 global-dynamic the
  `__tls_get_addr` result); after a park that may have migrated the fiber, the
  reuse touches the ORIGIN hub's copy (TSan A2: the select PRNG). Code that can
  park reaches a thread-local only through an out-of-line accessor
  (`RUNLOOM_NOINLINE`, or another TU like `runloom_mn_tls_current_g()`).
  Guard, **arm64 macOS only**: `tools/ci/check_tls_after_park.py` on the built
  .so (`tests/test_tls_after_park_lint.py`). Linux, the CI target, has no lint:
  only the out-of-line accessors protect it there.
- **Cooperative primitives are foreign-OS-thread-safe.** A non-goroutine thread
  (a patched `Lock` in an mp.Queue `_feed` thread) must detect no-goroutine (TLS
  peek NULL) and block on the real OS — never park a non-existent g, never lazily
  alloc sched state (`peek_current`, never `sched_get`).
- **CPython's locks that are held across Python code are owned by the thread
  state, and the `_thread` locks' waits park the fiber, through exec-home
  hunks whose hooks the library installs.** (1) Ownership: `Python/lock.c`
  keys `_PyRecursiveMutex` (so `threading.RLock`, and CPython's import and
  warnings locks) on the attached `tstate->id`, the fiber's own for its whole
  life; `Python/import.c`'s after-fork reinit uses the same identity. Keyed on
  the OS thread, a fiber that migrated inside the lock could not release it
  (wedged for good: an import or warnings lock hung every later import or
  warn), and a second fiber on the holder's hub re-entered it (two in the
  critical section). (2) Waiting: `Python/parking_lot.c` lets a wait flagged
  `_PY_LOCK_FIBER` (the lock objects' acquire, and RLock's `_acquire_restore`)
  ask `_Py_fiber_park` first; `runloom_fiberlock.c` installs those hooks at
  import (`STACKWEAVE_FIBER_LOCKS=0` leaves them out) and parks the fiber
  through `runloom_park_generic`, so its hub runs other fibers and the release
  wakes it from any thread. Without (2), a waiter blocked its hub's OS thread
  in the parking lot, and with as many waiters as hubs on a lock held across a
  park nothing could run the holder: a hard hang the deadlock detector cannot
  see (hubs busy, not idle); a waiter on the hub of a PREEMPTED holder hung
  that hub on its own, since nothing steals from a ready ring. `fl_prepare`
  names a fiber only while its own state is attached (M:N: `g->tstate`;
  `run(1)`: stackweave's interpreter): inside a subinterpreter call a fiber
  parked with a foreign state attached (`run(1)` crashed). A plain thread, the
  hub's own stack and a nested `run(1)`'s fibers keep the platform semaphore;
  nested fibers share the outer fiber's state, so they still re-enter its
  RLocks. `stackweave_c.rlock_tstate_owned` / `fiber_lock_hooks` say whether
  the interpreter has the lock.c and parking_lot.c hunks,
  `fiber_locks_installed` whether the hooks are in, and
  `stats()["fiber_lock_parks"]` counts waits that parked. Guards:
  `tests/test_fiber_locks.py`, `tests/test_cross_hub_migration.py -k
  stock_rlock`, `tests/test_cpython_thread_keyed_locks.py -k
  preempt_lock_holder`.
- **importlib's module locks key on the fiber, not the OS thread.**
  `import stackweave` rebinds `importlib._bootstrap._ModuleLock` to
  `_FiberModuleLock` (`src/stackweave/_importlock.py`): `_ModuleLock`'s own
  methods, run against importlib's globals with `_thread` swapped for one whose
  `get_ident()` is the running fiber (`current_g()`; the OS thread outside
  fibers) and whose `RLock`/`allocate_lock` are CoRLock/CoLock. Keyed on the OS
  thread, a second fiber on the importer's hub took the lock as its holder and
  got the half-built module, and an importer that migrated mid-body could not
  release it, so every later importer hung (big_100 p527; p458's false
  `_DeadlockError`). The locks must be cooperative too: a waiter blocking its
  hub's OS thread on a real lock never lets an importer parked on that hub
  finish. Never swap `_bootstrap._thread` itself: a lock taken before the swap
  is released after it as a different identity (the first `import stackweave`,
  made in a fiber, raised "cannot release un-acquired lock"); each lock must
  keep the identity it was made with. `has_deadlock` also fires when the holder
  cannot run until the waiter returns: the thread under a run(1) drain, the
  thread in `mn_run` (`stackweave_c._mn_run_thread`), the hub fiber under a
  nested run(1) or Coro (`_nest_owner`). Such an import gets the module so far
  (`import x`) or `_DeadlockError` (`importlib.import_module`), as between two
  threads, instead of hanging. Raw `stackweave_c` use without `import
  stackweave` gets none of this. importlib's global `_imp` lock is a
  `_PyRecursiveMutex`: owned by a fiber's thread state (the exec-home lock.c
  hunk), so a same-hub fiber no longer re-enters it. Guards:
  `tests/test_import_lock.py`, `tests/test_cross_hub_migration.py -k import`.
- **The stock-lock hooks park only where parking is sound, and only a FIBER's
  thread state owns a lock.** (1) Identity: `_PyRecursiveMutex_SelfIdent()`
  (the owner a `_PyRecursiveMutex` records, and every other lock keyed on the
  same identity) is the tagged `tstate->id` only for a state marked
  `_PyThreadStateImpl._fiber` (Py_TSTATE_EXEC_HOME, `_Py_TSTATE_FIBER_FLAG`),
  which spawn sets on every per-g state before its first attach
  (`runloom_iframe_mark_fiber_tstate`); every other state -- a thread's, a
  hub's, a C thread's per-callback PyGILState one -- keeps the OS thread, so
  code that never runs a fiber is exactly stock (an RLock taken in one
  PyGILState callback and released in the next still works). A new per-g state
  must be marked, or its locks key on whichever hub it runs on. (2)
  `fl_prepare` names a fiber only on the fiber's own coroutine (not inside a
  `_Coro` it resumed: the outer g would be marked parked while only the Coro
  yields), with its own state attached (not inside `_interpreters.exec`), and
  outside object destruction (`runloom_tstate_in_destruction`, the preemption
  gate); otherwise the wait blocks the thread, as stock. (3) Waiters: each is
  on the waiter list of the sched it waits on (`sched->fl_head`: a hub's for
  M:N, its owner's for run(1)), linked through its g, never its stack, under
  that sched's `fl_lock` -- per sched, so contended locks on different hubs
  share nothing. A wake holds that lock from before it queues the fiber until
  it has stored WOKEN, and freeing a g still on a list
  (`runloom_fiberlock_forget`, from `runloom_g_decref`: woken, then dropped
  unrun by a sched reset) unlinks it under the same lock first and gives back
  its run-alive count; so a wake never writes into a freed stack. (4) A wait
  ends with no wake credit outstanding (every credit then was aimed at it); a
  timed wait arms one timer entry (re-armed only after a migration) and, on
  ending, bumps `g->timer_ticket`, so the heap drops the entry instead of
  waking the fiber's next park and purges it before its deadline
  (`timer_stale`, `runloom_sched_drain_timers`). Out of memory arming it, the
  wait naps until woken or the deadline, its state detached so a
  stop-the-world does not wait on it. (5) Teardown: `mn_fini` ends the session
  first thing (`runloom_fiberlock_mn_stop`: a wake for one of its waiters is
  dropped from then on, never queued on a hub) and dequeues the waiters'
  parking-lot entries (`runloom_fiberlock_mn_unpark`); after the joins it
  waits out wakes still in flight (`fl_inflight`, a Dekker pair with
  `fl_alive`), dequeues late waiters too, unlinks them all and gives back
  their run-alive counts. (6) Fork: the child keeps the forking thread's
  run(1) waiters and wakes them to retry with a fresh parking-lot entry
  (CPython emptied the lot); the rest are dead and only their counts are given
  back. (7) Signals: a run(1) fiber's interruptible wait (`_Py_PARK_INTR`, the
  lock objects' acquire) on the main thread is woken by the drain while a
  signal is pending and returns `Py_PARK_INTR`, so CPython runs the handler in
  the fiber, as for a stock main-thread acquire; an M:N fiber never runs on
  the main thread, so like a stock non-main thread its wait is not
  interrupted, and the signal is carried out of `run()`. Known limitations:
  every lock park is foreign-wakeable (a plain thread may release), so the
  deadlock detector never reports a fiber-vs-fiber lock deadlock (AB-BA): it
  hangs; an RLock's owner is knowable (its thread state), a plain Lock's is
  not. And a lock handed off to a fiber that never runs stays locked for good:
  a release in the window between an aborted M:N `run()` returning and
  `mn_fini`, or one that reaches a fiber which began waiting while the hubs
  stopped, or a woken waiter that a sched reset then drops. Guards:
  `tests/test_fiber_locks.py` (`stats()` `fiber_lock_stale_wakes`,
  `_timer_arms`, `_signal_wakes`, `timer_entries`).
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
  the registry is unconditional, and ANY new
  spawn path that bypasses `runloom_greg_link` reopens the blind spot; (2) the
  single-thread drain's caller frames must stay registered via the base-snap
  registry (`runloom_base_snap_register`, one node per drain, paired at the single
  exit), and so must a parked `stackweave_c._Coro`'s saved state (its own node,
  linked at its first yield, unlinked when it finishes or is dropped, visited
  only while `parked`). Coro nodes sit on a list of their own, which a forked
  child keeps while it drops the drains' (`runloom_introspect_reset_after_fork`):
  the Coro objects survive the fork, and a Coro parked before it is parked in
  the child too. A Coro's frames link to whoever resumes it, as a
  generator's do: each yield cuts its bottom frame's `previous` off the
  resumer's frame and each resume re-links it (`runloom_iframe_chain_cut/_link`),
  so a parked chain holds the Coro's frames alone and the anchor never reaches a
  frame twice -- a twice-visited counted ref is subtracted twice. The cut walks
  the Coro's chain at every yield, except for a Python-function callable
  (`bottom_fixed`), whose one eval call spans its life, so the bottom frame
  found at its first yield is reused; a callable that can return to C between
  yields (a C callable, `partial`) must keep walking, or a cut lands on a dead
  frame. Guard:
  `tests/test_coro_frames.py` (`stats()["gc_coros_walked"]`); (3) the snap seam ordering L1–L5 (frozen in `runloom_sched_pystate.c.inc`
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

- **A hub-thread wake lands on the waker's OWN deque (Go-style local
  wake), not the global run-queue.** `runloom_mn_woken_enqueue`
  is the single routing point: hub-thread waker + unpinned g + not replay +
  deque not full -> owner push onto `cur->deque`; anything else -> global
  run-queue. Two consequences are load-bearing: (1) a deque can now hold a
  g in `wake_state == QUEUED` carrying a queue ref, so hub_main's pick step
  flags it (`from_runq = 1`) and the per-g resume block runs the same
  QUEUED->RUNNING claim + queue-ref drop as for a global pull -- any new
  deque/steal consumer must do the same or leak the ref and skip the claim;
  (2) `runloom_mn_any_stealable_work` counts a deque of ONE as surplus,
  because the owner may be mid-fiber and never reach its pick step --
  restoring the old `>1` there reopens a lost-wake vs park_enter.
  Guard: `tests/test_local_wake.py`.
- **A yield or an ended sleep re-queues where another hub can take it; a
  preemption does not.** `runloom_mn_requeue_local` puts an unpinned g its hub
  switched out still runnable (a yield, a sleep whose timer fired there, a
  fresh g the full deque refused) on `h->yieldq`, a Chase-Lev deque every
  consumer takes from the TOP (the owner too, so its order stays FIFO), and
  idle hubs steal from it. Two rules: (1) push only from hub_main, after the
  g switched out and its tstate is detached, never from inside the fiber: a
  thief resumes it the moment it is queued; (2) a preempted g goes back on the
  ready ring (`runloom_mn_preempt_yield`), which only its hub reads: a time
  slice ends wherever the fiber is, maybe inside a sqlite call or other C
  code that keys on the OS thread. A timer for a g pinned to another hub
  wakes it like a channel wake (QUEUED, global run-queue), so the pin holds.
  The ring is picked first, so the pick right after a preempted ring (or
  yield-queue) turn is the starve-bound forced turn (global run-queue,
  deque): without it a CPU-bound g refilled the ring every slice, and a g
  pinned onto its hub, which only the global queue holds and only that hub
  may take, waited 64 slices (~3 s) per turn. A g a forced turn picked hands
  the next pick back to the ring when preempted, so the two alternate: with
  every preemption forcing a turn, a stream of fresh CPU-bound gs (the deque
  pops LIFO) kept the ring from running at all. Guards:
  `tests/test_cross_hub_migration.py -k "idle_hub_takes or
  preempted_fiber_keeps or pinned_fiber_resumes"` (`stats()["mn_yield_steals"]`),
  `tests/test_sched_fairness.py -k "pinned_onto_one_hub or keep_arriving"`
  (`stats()["mn_preempt_turns"]`).
- **A fiber with a foreign C caller on its stack never moves to another
  hub.** A C library that calls back into Python through `PyGILState_Ensure`
  (sqlite3's user functions and handlers, ctypes / cffi callbacks, Cython,
  pybind11) may hold a lock owned by the OS thread across the call: sqlite's
  serialized-mode connection mutex aborted on macOS when a callback resumed on
  another hub. While the fiber's state has `gilstate_counter > 1`
  (`PyThreadState_New` gives 1), or Python asked for it
  (`runloom_mn_hold_here`, `g->hold_depth`), every switch-out holds it on its
  hub (`runloom_mn_hold_for_c_caller`, in hub_main's release BEFORE anything
  can route a wake): `pin_hub1` is derived as `cpin_hub1` if set, else
  `user_pin1` (spawn / `G.pin`), and only `runloom_mn_global_runq_set_pins`
  writes the three, under the runq lock. So its wakes take the global
  run-queue to this hub, and its timer and yields stay on the ready ring; a
  `G.pin()` made meanwhile applies after the callback returns. Callers that
  re-attach a saved state without the counter (`_tkinter`, `_ssl`'s password
  callback) are not covered. Guards:
  `tests/test_cross_hub_migration.py::test_identity_c_callback_keeps_its_os_thread_until_it_returns`,
  `tests/test_cpython_thread_keyed_locks.py::test_identity_sqlite_callback_keeps_its_os_thread_across_a_park`
  (`stats()["mn_c_caller_holds"]`).
- **monkey's sqlite busy wait never parks inside sqlite3_step.** The
  statement is suspended there, sqlite's busy handler is not reentrant, and
  serialized mode's recursive mutex lets another fiber of the hub into the
  same connection. The handler (`module_sqlite.c.inc`) only gives the wait
  back (SQLITE_BUSY) to an armed whole-call retry (`g->sqlite_coop`,
  `monkey/sqlite.py`), which sleeps outside sqlite -- held on its hub for a
  `check_same_thread` connection -- and otherwise sleeps as sqlite's default
  does. Only calls sqlite says may be retried are armed: a statement run in
  autocommit, or a commit; never a statement inside an open transaction, and
  never while a foreign C caller is on the stack (`gilstate_counter > 1`,
  checked both when arming and in the handler): SQL run from a user function
  is inside the outer connection's `sqlite3_step`, so it must not park
  either. Guards: `tests/test_monkey_sqlite.py`,
  `tests/test_cpython_thread_keyed_locks.py::test_preempt_sqlite_writer_waits_for_a_same_hub_transaction`.
- **A sysmon preemption request names the resume it is for.** sysmon asks two
  ways at once, a flag the eval-frame wrapper reads at the next call and a
  pending call run at the next backward jump; `h->preempt_seq` holds the
  wedged resume's `resume_seq`, and each path yields only while it still
  matches (`runloom_mn_preempt_due`). As a plain flag, whichever came second
  preempted the next fiber the hub ran before its first line, so a sibling on
  a CPU-bound fiber's hub never got a turn. Still open (KNOWN_GAP): a fiber
  preempted from the pending call is suspended inside CPython's
  `make_pending_calls`, which keeps the interpreter's pending-call slot
  (`handling_thread`) until it resumes, so a second single-frame loop cannot
  be preempted meanwhile. Guard:
  `tests/test_sched_fairness.py::test_preemption_takes_turns_between_fibers_on_one_hub`.
- **An M:N fiber's data-stack chunks come from its hub's pool and go back to
  it.** Each fiber has its own thread state, so unpooled, CPython mapped a
  16 KB chunk at the fiber's first call and unmapped it in
  `PyThreadState_Delete`: ~60% of a hub's busy time on no-op fibers. A Python
  fiber's entry (`runloom_g_entry`) installs one once, before its callable runs
  (`runloom_fiber_datastack_install`; a C-only fiber takes none). Not at a
  resume in hub_main: a callable that parks in C before pushing a frame was
  offered one again, and counted again, at every resume. hub_main releases
  them in the done branch, after the detach (`runloom_fiber_datastack_release`),
  through the same grace ring as the single-thread drain. A hub thread caps its
  pool at 512 (`RUNLOOM_CHUNK_POOL_HUB_CAP`); the grace ring is outside the
  cap. The dwell sweep reads an M:N
  fiber's chunk from `g->tstate`, not from its snapshot, which per-g fibers
  skip (so the sweep never reached one), under its sweep claim: hub_main
  publishes PARKED only after the fiber's brc_release and detach. Guards:
  `tests/test_fiber_datastack_pool.py` (`stats()["fiber_chunks_reused"]` /
  `_mapped`) and `tests/test_cov95_datastack.py::test_datastack_sweep_debug_decompose`
  (pages under 16 KB only).
- **The seeded M:N scheduler is disabled (TODO).** `STACKWEAVE_MN_SEED` /
  `STACKWEAVE_SIM_MN` runs are refused by `mn_init`: woken fibers run from the
  global run-queue, which the seeded baton does not order, so replay would not
  be deterministic. The controller is compiled in behind `RUNLOOM_MN_CTRL`
  (default 0) until it is re-implemented; its tests are `SEEDED_MN_TODO`
  xfails (`tests/known_gaps.py`).
- **A hub's io_uring ring has one producer: its own hub thread.** Under the
  loop backend (`STACKWEAVE_IOURING_LOOP`) a fiber may write SQEs only into the
  ring of the hub it runs on NOW, since every park is a possible migration.
  Single-shot ops (`runloom_iouring_loop_recv/send`) look up the current hub's
  ring at submit and take no ring argument. A multishot recv handle stays bound
  to the ring that opened it while its fiber migrates: the fiber reads chunks
  from any hub, and off the owner it posts buffer returns, re-arms and its close
  to the ring's inbox, which only the owner drains
  (`runloom_iouring_loop_inbox_drain`, at its loop top). Caching a ring across
  a park (the old all-C echo) lost SQEs and stranded fibers. Guard: `tests/test_cov100b_iouring.py::test_iouring_loop_echo_survives_fiber_migration`
  (and big_100 p223, which stalls within seconds without it). Multishot needs
  a provided buffer ring per hub and falls back to single-shot, with a one-time
  warning, when the kernel refuses one: `stats()["iouring_loop_ms_opens"]` /
  `_fallbacks` / `_pbuf_errno` tell the two runs apart, and
  `adv_util.kernel_pbuf_ring_errno()` asks the kernel directly. Migration with
  a stream open is opportunistic (a woken fiber moves only when stolen), so the
  guard reads `iouring_loop_ms_posted_returns`, repeats a run that had none
  (~1 in 800), and fails if four in a row had none.
- **Every provided buffer ring goes through `io_uring_l_pbuf.c.inc`** -- never
  call `IORING_(UN)REGISTER_PBUF_RING` directly. Ubuntu's 6.8 kernels (the bad
  backport is in 6.8.0-139's changelog; measured on 6.8.0-142) invert the
  reserved-word check in buffer-ring register AND unregister: zeroed `resv`
  gets EINVAL, nonzero goes through. The helper retries a refused 6.8
  registration with `resv[0] = 1`, and each ring unregisters in the form it
  registered with -- the plain form fails there, and freeing a ring the kernel
  still holds lets it write into reused heap (`iouring_pbuf_resv_quirk`;
  `STACKWEAVE_IOURING_PBUF_RESV_QUIRK=0|1` forces it off/on). Guard:
  `tests/test_cov100b_iouring.py::test_kernel_accepts_one_buffer_ring_registration_form`
  (the premise: a kernel takes exactly one form, and the inverted one only on
  6.8) and `::test_iouring_loop_echo_survives_fiber_migration[1]`.
- **The loop backend's ring is serviced every scheduling round, never only at
  hub idle.** `hub_main` calls `runloom_iouring_loop_poll` (submit deferred
  SQEs, post + drain completions, non-blocking) when its local queues run dry,
  BEFORE the global run-queue and stealing (Go's `netpoll(0)` in findrunnable),
  and every 64 turns while busy; `loop_wait` is just the blocking form for a hub
  with nothing to run. A hub fed by the global queue or stealing rarely idles,
  so idle-only servicing left ops unsubmitted and completions unread for whole
  busy stretches: 4-19x below epoll on p223, worse with more hubs. Single-shot
  `loop_recv/send` are direct-first (plain non-blocking syscall, ring only on
  EAGAIN; `STACKWEAVE_IOURING_LOOP_DIRECT=0` forces the ring), which is what
  brought single-shot to epoll parity -- the poll alone fixed multishot only.
  Guard: the same test asserts `stats()["iouring_loop_polls"] > 0`.

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

## monkey invariants (src/stackweave/monkey/)
- **The cooperative os.read/os.write family never changes an fd's
  O_NONBLOCK.** The flag lives on the open file description, shared by dups,
  child processes and every user of a terminal. A blocking fd is routed instead:
  socket -> `MSG_DONTWAIT` per call (recv; send off macOS, where it blocks);
  pipe/char device -> a zero-timeout level probe under the inode's stripe
  mutex, then the plain call (pipe writes in PIPE_BUF pieces); tty writes and
  unwatchable fds -> the pool. Never probe with `wait_fd` alone: it is
  edge-triggered, and a pipe still holding bytes from a consumed edge never
  fires again. Only `set_blocking` an fd stackweave created. Guard:
  `tests/test_cpython_thread_keyed_locks.py::test_identity_patched_os_read_leaves_the_fd_mode_alone`.
- **A fiber never calls a recursive multiprocessing SemLock's C object.** Its
  owner and recursion are keyed on the OS thread (ISMINE), so a migrated fiber
  waits on itself and cannot release. `_CoRecursiveSemLock` (the patched
  `SemLock._semlock`) keeps owner/depth per fiber and gates on a SEMAPHORE-kind
  twin of the same kernel semaphore; `SemLock.__enter__`/`__exit__` go through
  the instance's acquire/release. Guards:
  `tests/test_cpython_thread_keyed_locks.py::test_identity_mp_value_lock_reenters_after_a_migration[monkey]`,
  `tests/test_multiprocessing_compat.py`.
