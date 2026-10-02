# ThreadSanitizer: the gold lane, with migration on

How to run stackweave under ThreadSanitizer (TSan) on a free-threaded CPython
3.14 with both migration patches, what that run found on 2026-10-01, and what
TSan cannot see here. Migration is always on, so every M:N test below runs with
cross-hub fiber moves.

## TL;DR

- **TSan works with stackweave's fibers** on macOS arm64 (Apple clang 21). The
  scripts also handle Linux (`setarch -R`), but this run did not cover it. The
  fiber annotations already existed
  (`src/runloom_c/runloom_fiber_san.h`: `__tsan_create_fiber` /
  `__tsan_switch_to_fiber` / `__tsan_destroy_fiber` around the three swap sites
  in `coro.c` and `fcontext.c`). This lane fixed a bug in them (D1 below) and
  two problems that made TSan runs fail spuriously (D2, E1). It also adds a
  teeth check: a planted cross-hub race must be reported, and the same writes
  ordered by a park/wake and a migration must not be.
- **Two real races in stackweave's C** (A1, A2) and six benign ones (C1–C6). A1
  confirms dynamically the sleep-heap race that the PR #23 review had found by
  reading the code. A1, A2 and C1–C3, C5, C6 are fixed; C4 is accepted as it
  is. `tools/ci/check_tls_after_park.py` lints the release .so for A2's class.
- **The `ob_ref_local` question is settled.** A plain TSan run cannot answer
  it: CPython updates `ob_ref_local` with relaxed atomics, and TSan never reports
  a race between two atomic accesses. With the oracle below, which makes those
  updates visible, the `volatile` half of exec-home's `_Py_ThreadId()` turns out
  to be **load-bearing**. Without it, clang reuses a thread id across a call that
  parks the fiber. In `zip_next` that sends a `Py_DECREF` down the non-atomic
  owner path from the wrong thread, and TSan reports it. With the patch as
  shipped, there are no reports.

## Recipe

```sh
# 1. The interpreter (~4-6 min on an M-series Mac; make -j$JOBS)
PREFIX=$HOME/cpython-tsan SRC_DIR=$HOME/projects/cpython-tsan tools/build_tsan_cpython.sh 3.14.4

# 2. The lane (~4 min default; ~9 min with TSAN_GOLD_EXTENDED=1)
STACKWEAVE_TSAN_PYTHON=$HOME/cpython-tsan/bin/python3.14t \
STACKWEAVE_TSAN_CPYTHON_SUPP=$HOME/projects/cpython-tsan/Tools/tsan/suppressions_free_threading.txt \
    tools/run_tsan_gold.sh                 # or: tools/run_tsan_gold.sh tests/test_x.py ...
```

`build_tsan_cpython.sh` takes the python.org tarball, checks it against
`tools/cpython_pins.env` (`PY_TARBALL=` points it at a local copy), and applies
both `src/patches/` halves with `-F0`. It configures with `--disable-gil
--with-thread-sanitizer`, with no LTO and no PGO. It adds
`Py_TSTATE_ALLOC_HOME` / `Py_TSTATE_EXEC_HOME` to `CPPFLAGS` and appends them to
`pyconfig.h`. On Linux, configure, make and every run go under `setarch -R`.
`tools/ci/check_exec_home_tls.py` passes on the result (every `_Py_tss_tstate`
reader is in `pystate.o`).

`run_tsan_gold.sh` never touches the checkout. It copies the working tree
(tracked files plus untracked, non-ignored ones) to `$LOGDIR/tree` and builds the
extension there with **`STACKWEAVE_TSAN=1`**, the setup.py knob that adds
`-fsanitize=thread -g -O1 -fno-omit-frame-pointer` to compile and link. (A
separate build directory on `PYTHONPATH` would not do: several tests put `src`
first on `sys.path` in their subprocesses, and the checkout's normal `.so` would
silently win.) With the knob off, the build is unchanged. The fiber annotations
switch on from `__has_feature(thread_sanitizer)`. The runner then:

1. checks the `.so` is instrumented (`nm -u` shows `__tsan_func_entry`);
2. runs the teeth, `tools/verify/tsan_teeth.py race|clean`. The planted race
   must be reported in `pack_single`, the clean control must stay silent, and
   both must really run cross-hub;
3. runs each test file in its own process, under `perl -e 'alarm N'`;
4. prints every report's `SUMMARY` line, marked **KNOWN** (triaged in
   `tools/verify/tsan_gold_known.txt`) or **NEW**.

It exits **2** if the lane is broken (build, instrumentation or teeth) or any
file did not run normally. That covers a crash or other signal (rc ≥ 128), the
alarm (142), a pytest usage error or empty collection (4/5), an interrupted run
or internal error (2/3), a TSan `FATAL` / `CHECK failed` in any log, an
untriaged TSan deadly-signal report (`SEGV`, `stack-overflow`, ...), or a failing
test that is not on `tools/verify/tsan_gold_expected_fail.txt`. The log checks
matter because `exitcode=0` makes TSan exit 0 after a fatal report or a SEGV it
handled itself, so the return code alone shows nothing. Otherwise it exits **1** on any NEW
report and **0** when everything is KNOWN. The instrumented tree stays in the log
directory, so a single test can be rerun from it by hand.

Nothing in `src/runloom_c` is suppressed (`tools/tsan_suppressions.txt` explains
why). The only suppressions are CPython's own free-threading list.

By hand:

```sh
STACKWEAVE_TSAN=1 STACKWEAVE_EXTRA_CFLAGS="-DPy_TSTATE_ALLOC_HOME -DPy_TSTATE_EXEC_HOME" \
    $TSAN_PY setup.py build_ext --inplace --force
TSAN_OPTIONS="halt_on_error=0:abort_on_error=0:exitcode=0:report_signal_unsafe=0:history_size=7:suppressions=$CPY/Tools/tsan/suppressions_free_threading.txt:log_path=/tmp/tsan" \
PYTHON_GIL=0 PYTHONPATH=src $TSAN_PY -m pytest tests/test_mn.py -q
```

- `abort_on_error=0` is needed on macOS, where it defaults on. Without it, every
  process that saw a race exits with SIGABRT and a slow crash report, and
  subprocess-based tests fail on `rc=-6`.
- `exitcode=0` lets tests judge their own result; read races from the logs.
- `log_path` gives one file per process, subprocesses included.
- `history_size=7` is the maximum. With less, the other access of a race on a
  hub thread often keeps only a one-frame stack, and the SUMMARY names
  `__tsan_thread_start_func`; the full report still shows which finding it is.

**Cost** (18-core M-series Mac, measured): building the interpreter takes about
4–6 minutes (3.6 with `make -j18`) and the extension about 10 s. The default file set takes about 4
minutes and the extended set about 9 (`test_swarm_mn_sched` alone takes 5, and
8 on a loaded box).
Without TSan the same files take 55 s and 12 s. Most files run 3–10x slower
under TSan; the swarm file is about 40x slower, mostly because its subprocess
scenarios wait out their timeouts. A pytest process peaked around 0.6 GB RSS.

Tests that fail under TSan for reasons that are not bugs (none fail without
TSan):

- **Too slow for their budget on a loaded box** (all four passed on an idle
  one): `test_all_modes_at_once_under_hostile_workload_no_crash_no_hang`,
  `test_cpu_hog_plus_sibling_on_one_hub_both_complete` and
  `test_large_fiber_n_set_equality_at_scale[0]` hit their subprocess timeouts,
  and `test_sched_signal_woken_io_sleeper_survives_origin_heap_churn` misses its
  20-delivery floor or its 40 s watchdog.
- **TSan handles the fault itself:** `test_hub_guard_page_overflow_is_classified_not_silent`.
  TSan catches the deliberate guard-page overflow, prints its own
  `stack-overflow` report and exits with `exitcode`, which is 0 here.

All five are listed in `tools/verify/tsan_gold_expected_fail.txt`, so the
runner tolerates them. Any other failing test fails the lane.

## Findings (2026-10-01, 3.14.4 + both patches, macOS arm64)

Found at main @ 879fc099, then re-run after rebasing onto 9597d20c (#37, #40,
#41). The re-run reproduced the findings; C3 and C4 are intermittent, absent from
that run and present in earlier ones. The oracle runs were at 879fc099.

`tools/verify/tsan_gold_known.txt` pins two entries to a line number: the
`runloom_sched_sleep_until_ex` and `runloom_sched_ready_pop` sites, whose
functions have other, untriaged accesses (A1's `runloom_hub_main` entry left
with its fix). When that code moves, the report comes back as NEW, which errs
in the safe direction: update the line.
The pinned `file:line in function` keys were taken from macOS (`atos`)
symbolisation. On Linux, llvm-symbolizer's SUMMARY reads `path:line:col in
function`, so the line-pinned keys (and T1) will not match there and those
reports show as NEW -- the safe direction, but expect to add Linux keys. The
bare `in <function>` keys are portable.

Categories: **(a)** real data race in stackweave's C; **(b)** CPython race
reachable only because of migration; **(c)** benign or intentional; **(d)**
false positive or a TSan-environment problem.

| id | cat | where | what | severity | proposed fix |
|---|---|---|---|---|---|
| A1 | a | `runloom_sched_parkwake.c.inc` `runloom_sched_sleep_until_ex` (io path) -> `runloom_sleep_remove(target, g)` | A signal-woken io sleeper (CoPoll / `select` reprobe) is woken from the main thread through the global run-queue, so it can resume on another hub. It then removes itself from its **origin** hub's sleep heap (`target->sleep_heap[]`, `sleep_size`). That races the origin hub's `runloom_sched_sleep_pop` (hub_main:632), its `sleep_size` / heap-top peek (hub_main:628), and `runloom_sleep_push` from the origin hub's fibers. The code comment says this is safe because "the heap's only other mutator is this same hub thread" — but that assumes the fiber resumes on its own hub, which migration breaks. This is the PR #23 review item 7.5 #6, previously "confirmed by reading only"; TSan reports it in every run of `test_cross_hub_migration.py`. In one of seven direct TSan runs of that test's scenario, 6 sleepers were left about 59 s past their deadline and never woken. That is what a lost heap entry looks like, but it was not traced to this race, and the release build did not hang in 20 runs. | **High**, but rare: it corrupts the heap with plain stores on any architecture. A g can be duplicated (resumed twice) or lost (a sleeper never wakes). It needs a raising signal handler delivered into a parked io-sleeper. | **Fixed** (commit "sched: never edit another hub's sleep heap from a signal-woken sleeper (A1)"), by lazy deletion: only the owning hub edits its heap. The woken fiber leaves its entry behind; the signal wake posts the origin hub's purge mailbox and that hub drops the entry at its loop top (`runloom_sched_sleep_purge`). Entries are by value, so the heap never reads `g->wake_at`; each carries the sleep's `g->sleep_ticket`, a per-sleep generation that replaced `sleep_claimed`, so a stale entry cannot claim the fiber's next sleep; and each holds a g ref. Routing the wake back to the origin hub was rejected: it would tie delivery to that hub, so a wedged hub would hold the exception. `STACKWEAVE_DEBUG=sleepheap` aborts on any off-owner heap edit; `test_sched_only_the_owning_hub_mutates_its_sleep_heap` failed 10/10 before the fix. The lane no longer reports A1. |
| A2 | a | `chan_select_helpers.c.inc` `runloom_select_rng` (RUNLOOM_TLS), inlined into `runloom_chan_select` | The compiler computes a thread-local's address once per function and keeps it across calls; this is not Darwin-specific (see the lint section). In the macOS **release** `-O2` .so, `runloom_chan_select` makes one `tlv_get_addr` call at entry, spills the address to `[sp,#0x10]`, and reuses it on the `select_retry` path after `runloom_coro_yield`. After a migration, the fiber advances the **origin** hub's PRNG state, racing that hub's own selects (two fibers on two hubs, same address, unsynchronised). This is the exec-home bug class, in stackweave's own C. | **Low**: the value is PRNG state only. There is no memory-safety impact and no tearing on arm64. Select fairness is not measurably affected. | **Fixed**: `runloom_select_rand` is `noinline`, so every draw resolves the thread-local itself, right before using it, and `runloom_chan_select` no longer touches TLS at all (release .so: no `tlv_get_addr` call left in it; `runloom_select_rand` resolves and uses the address with no call in between). |
| C1 | c | `runloom_sched_drain.c.inc` `runloom_sched_logical_enabled` (`runloom_sched_logical_on`) | Lazy getenv cache, written and read as a plain int from any thread. | benign | **Fixed**: relaxed `__atomic_load_n` / `__atomic_store_n`, and the same for the identical cache in `runloom_sim_enabled` (`runloom_sim_on`), which C1's init calls. The release build's code is unchanged |
| C2 | c | `runloom_sched_preempt.c.inc` `runloom_preempt_init` / `_fini` vs `runloom_preempt_main` | The time-slicer stop flag `runloom_preempt_running` and `runloom_preempt_quantum_us` are plain ints read in another thread's loop. | benign (the loop calls an opaque sleep, so the read is not hoisted) | **Fixed**: relaxed atomics on both, `volatile` dropped. The release build's code is unchanged |
| C3 | c | `mn_sched_sysmon.c.inc` `runloom_sched_freeze_for_crash` | The crash handler stores `runloom_preempt_enabled = 0` as a plain int; the sysmon loop reads it. | benign | **Fixed**: a relaxed `__atomic_store_n` in the handler (a plain `str`, so still async-signal-safe), and relaxed loads in the sysmon loop and `runloom_preempt_install`. Code change: clang no longer narrows the flag to a byte (as a plain static int that only ever held 1 or 0 it could), so `strb`/`ldrb` become `str`/`ldr` |
| C4 | c | `module_g.c.inc` `RunloomG_stack` | `G.stack()` reads another fiber's `snap.valid` / `done` without synchronisation while that fiber parks on another hub (`runloom_pystate_snap`). | benign (diagnostic dict only) | **Accepted, not changed.** Making the read atomic means changing how `runloom_pystate_snap` writes `snap.valid`, which is part of the frozen L1–L5 snap seam (`valid=1` last and safepoint-free on snap). The worst case is a wrong `state` string in a diagnostic dict. It stays KNOWN in `tsan_gold_known.txt` |
| C5 | c | `runloom_introspect.c` `runloom_fiber_snapshot` vs `runloom_sched_sleep_until_ex:857` | The introspection read of `g->wake_at` is gated on an acquire-load of the state, but the state can be stale while the fiber is already writing its next deadline. | benign (an aligned double, diagnostic) | **Fixed**: relaxed generic `__atomic_store` at both `runloom_sched_sleep_until_ex` writes (M:N and single-thread) and `__atomic_load` at both readers (`runloom_fiber_snapshot`, `runloom_dump_fibers_fd`). The release build's code changes only in `runloom_fiber_snapshot`, which now copies the double through an integer register (`x15`) and zeroes it with `xzr`, at the same length. Since A1's fix the sleep heap keeps its own copy of the deadline, taken by `runloom_sleep_push` on the sleeping fiber's own thread right after its write (a plain read, which cannot race), so introspection is the field's only cross-thread reader |
| C6 | c | `runloom_sched_datastack.c.inc` `runloom_pct_init` vs `runloom_sched_ready_pop:375` | PCT's lazy init (test-only scheduler) writes `runloom_pct.*` while a hub's `ready_pop` reads `runloom_pct.enabled`. It shows up when a single-thread scheduler runs while M:N is live. | benign with PCT off | **Fixed**: `runloom_pct_init` runs under `pthread_once` and publishes `enabled` with release after the other fields; `ready_pop` reads it relaxed (the hot path's code is unchanged) and `runloom_pct_pick` acquires it before reading the rest. PCT stays single-thread only: with it on and M:N live, hubs would still share its counters |
| B1 | b | CPython `zip_next` (`Python/bltinmodule.c`), **only without exec-home's `volatile` `_Py_ThreadId()`** | See the oracle section: a wrong-thread non-atomic `ob_ref_local` write after a fiber migrates inside `tp_iternext`. | high on such a build | none needed: the shipped exec-home patch is clean. Keep the `volatile`. |
| D1 | d | `runloom_fiber_san_impl.h`, `coro.c` (fixed here) | A pooled coro reused its TSan fiber across goroutine lifetimes. `runloom_asm_entry` never returns, so each lifetime left one frame on that fiber's shadow stack. Report stacks filled with repeated `runloom_asm_trampoline` frames, and after about 64K reuses of one coro TSan would write past the end of the shadow stack. | lane bug | Fixed: one TSan fiber per goroutine lifetime. `runloom_fibersan_left` retires it when the goroutine is done, `runloom_coro_destroy` retires it on the pool-recycle path as well (a Python-level `Coro` deallocated or re-initialised mid-body is pooled unfinished), and `enter()` creates a fresh one. |
| D2 | d | `runloom_sched.h` (fixed here) | A 256 KB fiber is too small under a TSan interpreter, whose frames are inflated and whose stack margin doubles: a deep import chain in a fiber raises `RecursionError: Stack overflow (used 144 kB)`. | lane bug | Fixed: sanitizer builds floor fiber stacks at 1 MiB. The gate is `RUNLOOM_SANITIZED` (plat.h), which ASan sets too, so the ASan lane gets the same 1 MiB floor. |
| E1 | — | `runloom_iframe.c` `runloom_arm_fiber_stackprot` (fixed in #43) | `PyUnstable_ThreadState_SetStackProtection` returns -1 and sets `ValueError` when the window is below the **interpreter's** `_PyOS_MIN_STACK_SIZE` (192 KB on a TSan CPython, 48 KB on a release one). The return value was ignored, so the error leaked into the fiber's next C call as `SystemError: ... returned a result with an exception set`. It hung `test_once_executor_sees_exception_others_dont`. Not a race. | default build unaffected (its windows are always above 48 KB) | Fixed: clear the error and fall back to the raw arm. |

No category (b) report appeared with the shipped patches: no CPython-internal
race was reachable because of migration. Every CPython-side report the lane
printed was suppressed by CPython's own list.

T1 in the known list is not a race. `test_swarm_mn_sched` overflows a hub's
guard page on purpose, and TSan reports the overflow itself.

### Results by file (gold lane, extended set, rebased main)

Each file runs in its own process.

| file | result under TSan | findings reported |
|---|---|---|
| teeth race / clean | planted race reported / silent | — |
| test_mn | 9 passed | A2 |
| test_local_wake | 4 passed | none in this run (C4 in earlier runs) |
| test_cross_hub_migration | 17 passed, 3 xfailed (the known gaps) | A1, C1, C5, C6 |
| test_tlbc_parked_frame_gc | 5 passed | none |
| test_fiber_tstate_isolation | 9 passed | none |
| test_chan / test_chan_stress | 26 / 8 passed | — / A2 |
| test_sync / test_sync_primitives | 7 / 20 passed (`test_sync_primitives` hung before the E1 and D2 fixes) | — / C1 |
| test_mn_park / test_mn_teardown | 8 / 2 passed | none |
| test_swarm_mn_sched | 70 passed, 3 xfailed, 1 failed (`test_hub_guard_page_overflow_is_classified_not_silent`, see above) | C1, C2, T1 (C3 in earlier runs) |

### Re-run after the fixes

With A1, A2 and C1–C3, C5, C6 all fixed, the lane reports nothing on the files
that carried them (exit 0):

| file | result under TSan | findings reported |
|---|---|---|
| teeth race / clean | planted race reported / silent, both cross-hub | — |
| test_mn | 9 passed | none (A2 before) |
| test_chan_stress | 8 passed | none (A2 before) |
| test_cross_hub_migration | 19 passed, 3 xfailed | none (A1, C1, C5, C6 before) |

The C fixes were also run before A1 landed, on two more files:
`test_sync_primitives` (20 passed, no reports; C1 before) and
`test_swarm_mn_sched` (69 passed, 3 xfailed, 2 failed, both on
`tsan_gold_expected_fail.txt`; T1 only; C1 and C2 before). C3 had been
intermittent, so its absence from a run is weak evidence on its own; the fix
makes the store and both loads atomic, which TSan does not report.

## The `ob_ref_local` oracle

Exec-home's `_Py_ThreadId()` half (`__asm__ __volatile__` on the thread-id
read) exists so that a stale thread id cannot send a refcount update down the
owner-only, non-atomic `ob_ref_local` path. `src/patches/README.md` recorded
that no failure had been pinned on that half, and that settling it "needs
ThreadSanitizer on `ob_ref_local` under migration".

Plain TSan cannot answer this. Every `ob_ref_local` access is a relaxed atomic
load or store, and TSan never reports atomic-vs-atomic, so lost updates are
invisible to it by construction.

`tools/verify/tsan_refcount_oracle.py` (`ORACLE=1` in `build_tsan_cpython.sh`)
makes them visible:

- the **owner-path** store becomes a plain, instrumented store;
- every read made before the ownership check (the immortality test,
  `Py_REFCNT`, `_Py_IsImmortal`, `_Py_TryIncrefFast`, `_PyObject_ResurrectEnd`)
  goes through an uninstrumented helper.

Two owner-path stores from different threads with no happens-before between
them then report as a write/write race. `ORACLE=plain-tid` also drops the
`volatile`.

| check | `ORACLE=1` (shipped exec-home) | `ORACLE=plain-tid` |
|---|---|---|
| teeth: `tsan_oracle_teeth.py` (planted wrong-thread `Py_IncRef`) | reported (`Py_IncRef`, object.c) | — |
| migration canaries, smoke, `test_cross_hub_migration`, `test_mn`, `test_chan_stress`, `test_local_wake`, `test_swarm_mn_sched` | no `ob_ref_local` report | no `ob_ref_local` report |
| `tsan_oracle_zip_canary.py`, 4 runs each (190–310 cross-hub resumes per run) | **0 reports** | **reported in 3 of 4 runs**: `zip_next` bltinmodule.c:3197 `Py_DECREF(olditem)`, a 4-byte write racing a churner fiber's owner-path write (in one run TSan named the churner's side, `list_get_item_ref`, as the SUMMARY) |

Static evidence, from compiling `Objects/*.c` and `Python/*.c` with release
flags (`-O3`, no TSan, Apple clang 21), with and without the `volatile`:

- Without it, 409 of 7136 thread-id reads disappear.
- In `_PyEval_EvalFrameDefault` that changes nothing that matters. The reads
  that go are CSE'd within one specialised instruction (`FOR_ITER_LIST`,
  `LOAD_ATTR_*`, `LOAD_GLOBAL_MODULE`, ...), and the only call they cross is
  `_Py_TryIncrefCompareStackRef`, which cannot run Python.
- In 29 functions, a thread id is reused **after a call**. In several of them
  the call can run Python and so park the fiber:
  - `zip_next`, across `tp_iternext`;
  - `_Py_dict_lookup_threadsafe`, across `PyObject_RichCompareBool`;
  - `PyObject_CallFinalizerFromDealloc`, across `tp_finalize`;
  - `func_dealloc` / `code_dealloc` / `_PyFrame_ClearExceptCode`, across
    `PyErr_FormatUnraisable` or a nested dealloc.

  `zip_next` is the one the canary drives.

**Conclusion: keep the `volatile`.** It is load-bearing, it is free (per the
cost table in `src/patches/README.md`), and the oracle shows it closes a real
wrong-thread refcount path.

## What TSan cannot see here (read before trusting a clean run)

- **Incidental synchronisation hides races.** Every free-threaded refcount RMW
  on a shared object is a TSan release+acquire. Two loops that both touch a
  shared object are mostly ordered by it, so a planted race needs about 10^5
  iterations to surface. A real race has to slip between two such edges, so a
  clean run is weak evidence for code that also does refcounting.
- **Same-hub fibers are serialised.** `__tsan_switch_to_fiber(..., 0)`
  synchronises at every switch, so two fibers interleaved on one hub are always
  ordered. That is the correct model for cooperative switches, but only cross-hub
  (truly parallel) races are in scope.
- **Each report is printed once per pair of stacks.** Counts are not
  frequencies.
- **Relaxed-atomic protocols** (`ob_ref_local`, the stackweave fields that are
  already `__atomic`) are invisible unless something like the oracle makes one
  side plain.
- **TLS address caching (A2)** shows up only when two hubs actually touch the
  same TLS block. The systematic check is the lint below, on macOS only.

## The TLS-after-park lint

`tools/ci/check_tls_after_park.py [-v] [EXTENSION.so]` (arm64 macOS; default:
the extension built into `src/`) reads the release extension's machine code
and fails when a thread-local's address, or the thread pointer, is used after a
call that can park the fiber. That is A2's class, and B1's for the inlined
`_Py_ThreadId()` reads. It runs in about 2 s. `tests/test_tls_after_park_lint.py`
runs it on the built extension and on small fixtures that must flip its
verdict (a reused TLS address against a noinline accessor; the thread pointer
read with and without `volatile`; the address passed again to an out-of-line
helper, directly and as a tail call, against a helper that resolves the
thread-local itself), so it runs in the `tests` phase of
`scripts/check_all_fast.sh` on a Mac.

How it decides, in short (the module docstring has the details):

- **Sources**: a call through a `__thread_vars` TLV descriptor, a
  `mrs TPIDRRO_EL0`, or a call to an image function that returns a TLS address.
- **May-park calls**: anything that reaches `runloom_coro_yield`, the only
  fiber-side suspension (every `runloom_asm_swap` caller is checked against a
  fixed set); an indirect call; a Python C API call (it can run Python code: a
  finalizer, a callback, the preemption hook), except a short reviewed list
  that cannot; a libc call that calls back into the image.
- **Uses**: a forward data-flow pass follows the address through registers,
  FP/SIMD registers, stack slots and pointer arithmetic. After a may-park call
  it is stale, and a stale value reaching an address, a compare, a call
  argument, a store or a return value is a finding. A fresh address stored to
  non-stack memory or passed to a may-park call is a finding too, because the
  check cannot follow it.
- **Exit 2** when it cannot vouch: not arm64 Darwin, a stripped image, an
  instruction, jump table or TLV form it does not model in a function that
  touches TLS, or no TLS access found in `runloom_mn_tls_current_g`.

Results (Apple clang 21): before the A2 fix the release build reports four
stale uses in `runloom_chan_select` (`runloom_select_rng`, loaded and stored
after `runloom_coro_yield`) and exits 1. With the fixes it exits 0 on the
release (`-O2`), `-O3`, `-Os`, `STACKWEAVE_DEBUG=1` (`-O0`),
`STACKWEAVE_TSAN=1` and `STACKWEAVE_CTXCHECK=1` builds, and on an
aggressive-inlining build (`-mno-outline -mllvm -inline-threshold=1000`).
The aggressive builds check that the fixes do not lean on today's inlining
choices. Before the out-of-line accessors the threshold-1000 build reported 20
stale uses: `runloom_sched_get` inlined into `runloom_park_until(_locked)`, the
chunk pool and grace ring into `runloom_sched_drain`, and the g slab into
`spawn_common`. At threshold 5000, `runloom_mn_fiber_core` was inlined into
`runloom_mn_fiber_n`'s bulk-spawn loop, with 28 stale uses of `tls_hub`,
`tls_current_g`, the pace and fast-path counters, `self_queued` and
`steal_rng` across `runloom_g_decref` (a stale `tls_hub` pushes onto the
origin hub's deque as its owner). The CTXCHECK build had 481, from the inline
lockrank push/pop, the same class in debug-only code. Those functions
(`runloom_sched_get`, `runloom_g_slab_alloc/free`,
`runloom_chunk_pool_get/put`, `runloom_parker_pool_release`,
`runloom_mn_fiber_core`, and the lockrank and CTXCHECK helpers) are now
`RUNLOOM_NOINLINE`. Measured: clean up to threshold 1000; at 5000 the 11
reports left are the two inlining limits below, and none is an unaccepted
reuse.

Cost: in the default build, callers that inlined these accessors now call
them. That covers `runloom_sched_get` in the single-thread park and wake paths
and in `runloom_sched_set_default_stack_size` and `runloom_cal_record`; the
slab in `spawn_common` and `runloom_g_decref`; the chunk pool in the drain;
and the parker release in `runloom_netpoll_wait_fd`. Single-thread channel
ping-pong is about 1.2% slower, which is significant (a second, paired 21-round
A/B here gave +0.7%, 14 of 21 rounds slower). Spawn and yield are within noise
(`sleep(0)` +0.9% paired). `runloom_mn_fiber_core` was already out of line in
the default build, so its attribute changes no default code.

Two reviewed lists keep it at zero:

- `NATIVE_ONLY`: `runloom_hub_main`, `runloom_blockpool_worker` and
  `py_blocking_worker_thread_fini` run only on their own OS thread's stack, so
  nothing they call can move their frame. `hub_main` alone has 18 reuses that
  would otherwise be reported. The check fails if one gains a direct caller.
- `ACCEPTED`, keyed by function and variable, so anything new there still
  fails. `runloom_sim_dispatch_due_plane` re-reads `runloom_sim_due_scratch`
  after each dispatch. That is simulation mode only, reached on a fiber
  through `netpoll_poll()`, and it parks only if a woken g's last decref runs
  a finalizer that parks. It is real only in that narrow case and is left as
  it is. The lint notes an `ACCEPTED` entry that matches nothing.

### Platforms

The class is not Darwin-only. The same source compiled for Linux (clang
`-O2 -fPIC`, a thread-local PRNG drawn in a loop around
`runloom_coro_yield`) keeps the thread-local's location across the call
too:

- aarch64, initial-exec (the release model): `mrs x22, TPIDR_EL0` is hoisted
  out of the loop and every access is `[x22, x21]`, so it uses the old
  thread's thread pointer;
- aarch64, global-dynamic (the sanitizer model): the TLSDESC result and
  `TPIDR_EL0` stay in `x21`/`x22`;
- x86-64, global-dynamic: the `__tls_get_addr` result stays in `r15`;
- x86-64, initial-exec is the one model that re-reads the thread pointer at
  each access (`%fs:(%r14)`, only the offset is kept), but a materialised
  address, `&var` passed to a helper, is not.

The lint reads arm64 Mach-O only. Linux, the blocking target in CI and built
with GCC there, has no lint: only the out-of-line accessors protect it, by
construction.

### Limits

- Pointer arithmetic other than add/sub, and/orr/bic, madd and selects is
  assumed to produce a non-pointer.
- It follows data flow, not equalities: after `if (x == tid)` the compiler may
  use `x` for `tid`.
- A TLS address passed to a call that cannot park is not followed into the
  callee (`-v` lists those calls).
- An address stored in a stack struct whose address is passed to a callee that
  parks and then uses it is not seen. The struct escapes as a stack pointer,
  not as a TLS address.
- Only x0 carries a returned TLS address: one returned in `x1` (a two-pointer
  struct) is lost.
- Values loaded from a thread-local (a cached pointer such as a hub or sched
  read once and used after a park) are out of scope. They are values, not the
  thread-local's address, and whether a stale one is wrong is the code's
  semantics, not a codegen question.
- It is path-insensitive. At `-O1` it reports 22 stale uses in
  `runloom_mn_fiber_core` for `runloom_steal_rng`: a register that holds the
  address on one path reaches its uses on another path that the program
  cannot take.
- Nothing inside the Python C API or libc is checked.
- `-Oz` (machine-outlined helpers with control flow), `-flto=thin` (the swap
  inlined into new callers) and GCC builds (emulated TLS, no `__thread_vars`
  descriptors) are not modelled: the lint exits 2 on them rather than vouch.
- A register-indexed load from the frame (`ldr x0, [x9, w8, uxtw #3]`, a stack
  array) cannot be keyed to one slot, so it takes the join of every spilled
  slot. At threshold 5000 that gives 7 false positives in `runloom_sched_drain`
  (`runloom_tls_sched`, which is spilled elsewhere in the frame).
- `ACCEPTED` is keyed by function. At high inline thresholds the accepted
  `runloom_sim_due_scratch` exposure is inlined into `runloom_netpoll_pump` and
  `runloom_sim_dispatch_due`, so it is reported again there (4 at threshold
  5000). That fails closed: the lint never accepts a reuse under a name nobody
  reviewed.

## Not covered by this run

- Linux. The scripts handle `setarch` but were not rerun there, and the Docker
  lane was out of scope.
- The io_uring loop, `STACKWEAVE_GON_BULK`, monkey/aio, and the network and
  stress suites (`test_swarm_aio_bridge`, `test_adv_aio`, servers): not run.
- The oracle with the full suite, and the oracle on 3.15.
- `tools/verify/tsan_gold_drift.py` was not updated. Its baseline records a
  clean gold run, and this run is not clean (A1, A2, C1–C6).
