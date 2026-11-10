# CPython patches for stackweave

Cross-hub fiber migration needs **both** halves below. They fix independent
halves of the same root cause — free-threaded CPython ties a running frame to
one OS thread in two separate ways — and either alone still corrupts:

| half | flag | what it decouples |
|---|---|---|
| **allocation** | `Py_TSTATE_ALLOC_HOME` | which heap a migrated fiber allocates on |
| **execution** | `Py_TSTATE_EXEC_HOME` | whether the compiler may cache *which OS thread we are* (plus memoryview's and array.array's buffer-export counts, which a migrated fiber's deallocs race, and the locks CPython holds across Python code -- `_thread` locks, the import and warnings locks, buffered streams, multiprocessing's RLock: owned by a fiber's thread state rather than the OS thread, and their waits can park a fiber; and a hook that lets a fiber be preempted without a PEP 523 eval-frame function, see below) |
| execution, own flag (on in the repo's builds) | `Py_TSTATE_TLBC_FOLLOW` | whether a frame follows its thread state's thread-local bytecode copy, so fibers can share their hub's copy instead of owning one each (needs `Py_TSTATE_EXEC_HOME`; costs ~4%, see below) |

Each half ships as a **version-specific** patch (stackweave supports only
free-threaded CPython 3.14+), and they are **not** interchangeable across
series — take the pair matching your interpreter:

| target | allocation | execution |
|---|---|---|
| **CPython 3.14.4t** | `cpython314t-tstate-alloc-home.patch` | `cpython314t-tstate-exec-home.patch` |
| **CPython 3.15.0t** | `cpython315t-tstate-alloc-home.patch` | `cpython315t-tstate-exec-home.patch` |

All four apply at **zero fuzz** (`patch -p1 -F0`) to their pinned release, and
`tools/ci/check_patches.sh` enforces that in seconds. The cross-version deltas
are not cosmetic: the patches were first written for 3.13 (removed with 3.13
support; in git history), and 3.14 reordered `_PyThreadStateImpl` and rewrote the
mimalloc page-reclaim path, so the 3.14 alloc-home patch **drops** two hunks
upstream has since superseded and **adds** one relaxing an assert that alloc-home
makes false. 3.15 moved `_Py_ThreadId` from `Include/object.h` to
`Include/cpython/object.h`, so the 3.15 exec-home patch targets the new header
(same asm transform). See each patch's `WHAT CHANGED` header. Applying another
series' patch with `-F3` can fuzz superseded hunks back in and yield a silently wrong
interpreter — so don't.

Every M:N run migrates: run `run(n > 1)` on an interpreter missing either half
and a migrated fiber can corrupt memory under churn. stackweave says so but
doesn't refuse: `import stackweave` warns when the extension was built for
another patch set than the running interpreter's, and `mn_init` warns once at
2+ hubs when the extension was built without both
(`stackweave_c.migration_patched` is 0).

## `cpython314t-tstate-alloc-home.patch` — per-tstate allocation home

Optional free-threaded CPython feature (`-DPy_TSTATE_ALLOC_HOME`, **off by default**) that
lets a thread state borrow another tstate's allocator. It is the minimal,
upstream-shaped enabler for **transparent cross-hub migration of real stackful
fibers** — the thing that is otherwise blocked because one `_PyThreadState_GET()`
ties execution to allocation (see `docs/dev/HUB_MIGRATION_VERDICT.md`).

- **Default build:** zero change — the redirect macro-expands to the original
  `tstate->mimalloc`; no field, no ABI change, no perf cost.
- **Enabled build:** a migratable fiber runs under a thin execution-only tstate
  whose `_alloc_home` is repointed (one store) to the current hub on each resume.
  Allocation lands on the running hub's heap; old objects remote-free (supported).
  No per-fiber heap (no GC wall), no heap migration (no `_mi_page_retire` crash).

**stackweave wiring (when enabled):** call `_PyThreadState_SetAllocHome(g->tstate,
hub->tstate)` at the per-g-tstate attach point (`mn_sched_hub_main.c.inc`); give
the per-g tstate no live heap. That turns per-g-tstate migration from
"heavy + crashing" into "lightweight + sound".

**Validation status: VALIDATED end-to-end.** Built CPython 3.14.4t with the flag
(593 stdlib tests pass, zero regression). Wired `runloom_iframe_borrow_alloc_home`
into the per-g-tstate attach (`mn_sched_hub_main.c.inc`). The previously-crashing
per-g-tstate channel-churn repro now passes **24/24** with the borrow vs
**8/8 abort** without it.
Refined scope: only the alloc-heap + mimalloc page_list redirect to home; the QSBR
reader stays the running tstate's (`_Py_qsbr_poll` asserts that). A direct migration
proof (`tests/experiments/resume_rebuild/migration_crosshub_proof.py`) shows **50/60 fibers
wake on a different hub than they parked on, zero crash** — real cross-hub migration
of transparent stackful fibers.

**Scope limit:** this patch moves the *heap*. It does nothing about the compiler
caching *which OS thread is running* — see the exec-home patch below, which is
required alongside it.

## `cpython314t-tstate-exec-home.patch` — non-cacheable execution-context reads

Optional CPython feature (`-DPy_TSTATE_EXEC_HOME`, **off by default**) that stops
the optimizer from caching the two reads identifying the OS thread a frame runs
on: `_PyThreadState_GET()` (via `pycore_pystate.h`, now routed through the
out-of-line `_PyThreadState_GetCurrent()`, which gains `Py_NO_INLINE` so no
caller can inline it) and `_Py_ThreadId()` (via `object.h`, whose per-arch reads
become volatile asm).  The scheme as a whole does not survive LTO -- see
"Build flags" below.

**Why it's needed.** Both reads are pure expressions, so the compiler hoists them
out of loops, CSEs repeats into one, and sinks them to function entry. That is
sound under C's assumption that a live frame stays on one OS thread — an
assumption a stackful fiber scheduler breaks by swapping C stacks. The result is
a use-after-free with no incorrect C anywhere in the source:

- a stale `_PyThreadState_GET()` drives another thread's exception state /
  recursion limits / delayed-free queue, or one already freed by
  `PyThreadState_Delete()` if the origin hub has exited;
- a stale `_Py_ThreadId()` makes `_Py_IsOwnedByCurrentThread()` answer "yes" on a
  thread that doesn't own the object, routing decrefs down the **non-atomic**
  `ob_ref_local` path. Racing threads there lose or duplicate decrements; a
  duplicated one frees an object still referenced elsewhere.

**Not a Darwin problem.** The manifestation is platform-specific; the bug is not.
Compiling a read of the thread-local twice around an opaque call that can migrate
the fiber (clang `-O2`) — the second read is the one that must see the new thread:

| target | what the compiler does to the second read |
|---|---|
| Darwin/AArch64 | caches the **address**: one `tlv_get_addr` per function, spilled to the stack frame and reused |
| Linux/x86-64 | caches the **value**: `movq %fs:tss_tstate@TPOFF, %rbx` before the call, `movq %rbx, %rax` after — the second read is deleted |
| Linux/AArch64 | same: `mrs x8, TPIDR_EL0; ldr x19, [x8]` before, `mov x0, x19` after |

Identical under `-ftls-model=` `local-exec`, `initial-exec` and `global-dynamic`.
The Linux form is arguably worse — no address is involved at all, so there is
nothing to "resolve correctly"; the value simply predates the migration. It is a
legal optimization: the slot's address never escapes, so LLVM concludes no other
thread can write it — true only while a frame stays on one thread.

`_Py_ThreadId()` is worse still, because its per-arch reads are plain
(non-`volatile`) asm. On **all three** targets clang deletes *both* reads around
the call and folds `tid_before == tid_after` to a compile-time `mov w0, #1` —
"same thread", decided at compile time. Patched, each emits two real reads.

*(Linux numbers here are cross-compiled codegen only; the runtime soak below was
run on arm64 Darwin.)*

**The crash, in situ.** In `_PyEval_EvalFrameDefault` (3.14.4t, `-O2`, clang 21,
Darwin/arm64) the faulting sequence is:

```
ldr x9,  [sp, #0xf8]     ; cached &_Py_tss_tstate  <-- ORIGIN hub's slot
ldr x20, [x9]            ; tstate
ldr x9,  [x20, #0x350]   ; tstate->c_stack_soft_limit   <-- SIGSEGV, x20 == 0
```

The frame is on the *fiber's* stack, so it travels to whichever hub resumes the
fiber and keeps reading the origin hub's slot. Caught in the act: the crashing
thread's own `&_Py_tss_tstate` was `0x100cd84b8`, while the address cached in its
eval frame was `0x100cd6ff8` — a **different** hub thread's slot, and that thread
was parked idle in `cvwait`, so the slot held NULL. Had it instead been running
another fiber, the read would have returned a valid-but-wrong tstate and corrupted
state silently. The same fault reproduces as `_PyCriticalSection_BeginMutex`
(offset `0xb0`) and `_PyErr_Occurred` (offset `0x70`) depending on which inlined
`_PyThreadState_GET()` the optimizer happened to reuse.

**Default build:** zero change — `_Py_TID_ASM` expands to plain `__asm__` and
`_PyThreadState_GET()` keeps its inline thread-local read, both behind `#ifdef`.
No ABI change. Enabled, it costs exactly the hoist/CSE the inline reads exist to
enable.

**Validation status: VALIDATED end-to-end** (3.14.4t, arm64, release `-O2`, with
alloc-home applied). `benchmark/bench_migration.py` **before**: `--hubs 1` clean,
`--hubs 2/4/8` SIGSEGV every run. **After**: `--hubs 1/2/4/8` all clean, 10/10 soak
runs at 8 hubs × 64 fibers × 60 rounds × 32 allocs, with **77–80% of wakes landing
on a different hub than the fiber parked on**. `mpmc_pergt_repro.py` and
`migration_crosshub_proof.py` both pass; stackweave's own suite is unchanged (223
passed / 1 skipped, and the two residual failures — `test_mn_sim_bytes` late-parker,
`test_monkey_leak` subprocess — reproduce identically on the *unpatched*
interpreter, so they are pre-existing). **Not** run against the CPython test suite.

alloc-home is genuinely required alongside exec-home: with the heap borrow off
(exec-home on, alloc-home off) `mpmc_pergt_repro.py` still crashed **3/8** runs vs
**0/8** with both.

### Which exec-home half fixes what

exec-home has two halves, and they are *not* equally evidenced. Three interpreters,
identical apart from the patch, running `exec_home_min.py --hubs 4`, 10 runs each:

| build | halves present | result |
|---|---|---|
| `.venv-orig` | neither | **SIGSEGV 10/10** |
| `.venv-halfa` | `_PyThreadState_GET()` only | clean 10/10 |
| `.venv-mig` | both | clean 10/10 |

**The crash is entirely attributable to the `_PyThreadState_GET()` half.** The
`volatile` `_Py_ThreadId()` half contributes nothing to fixing it.

That half is kept on different grounds. Its mechanism is concrete and readable in
`refcount.h` — a stale thread id mis-answers `_Py_IsOwnedByCurrentThread()`, sending
a refcount update down the **non-atomic** `ob_ref_local` path from a thread that does
not own the object, which loses updates when the real owner does the same thing
concurrently. But **no failure has been pinned on it**. A canary hammering
main-thread-owned objects from migrating fibers came back clean on all three builds
(its first version reported drift on all three, including the fixed one — a
self-inflicted false positive from `for o in SHARED` leaving `o` bound). A lost
refcount update is silent, so a clean run cannot distinguish "unnecessary" from "not
yet observed". It is kept because it measured **free** and covers the failure class
testing cannot rule out. Settling it properly needs ThreadSanitizer on `ob_ref_local`
under migration; that has not been run.

**Cost.** Measured against the **alloc-home-only** build — i.e. this is the marginal
cost of adding exec-home to the original patch, not the cost of migration support
as a whole. Three interpreters built identically apart from the patch;
interpreter-bound microbenchmark, 2M iterations, min of 5, 3.14.4t arm64:

| | alloc-home only | + exec-home | Δ |
|---|---|---|---|
| dict alloc churn | 0.152 s | 0.176 s | +16% |
| function call | 0.042 s | 0.045 s | +7% |
| list alloc churn | 0.112 s | 0.124 s | +11% |

All of it is the `_PyThreadState_GET()` out-of-lining: the half-only build measures
0.175 / 0.044 / 0.125 s, indistinguishable from both halves. The `volatile`
`_Py_ThreadId()` is free. Anyone making migration cheaper should target the tstate
lookup, not the thread id.

> An earlier revision of this file quoted +38/+15/+52%. That was wrong: it compared
> against a separately-built interpreter rather than a controlled baseline, so build
> differences were being counted as patch cost. The table above compares three
> interpreters built from the same source with the same configure line.

**⚠ Build flags: no LTO.  PGO alone is fine.**  exec-home keeps every read of
the `_Py_tss_tstate` thread-local behind a call into `Python/pystate.c`, so no
caller outside that file holds a read the compiler may cache across a fiber
park.  The rule that matters is "no direct `_Py_tss_tstate` read outside
`pystate.c`", and only cross-TU inlining can break it.  Measured on arm64 Darwin
(Apple clang 21 / clang-2100.1.1.101, 3.14.4 + both patches,
`tools/ci/check_exec_home_tls.py <python>`):

| configure | functions reading `_Py_tss_tstate` (directly or via an outlined helper) | `_PyThreadState_GetCurrent()` call sites | `PyThreadState_Get()` call sites |
|---|---:|---:|---:|
| (none) | 28, all in `pystate.c` | 2059 | 473 |
| `--enable-optimizations` | 26, all in `pystate.c` | 1802 | 485 |
| `--enable-optimizations --with-lto=thin` | **413** | 5508 | **7** |

`Py_NO_INLINE` holds under LTO: `_PyThreadState_GetCurrent()` itself is never
inlined.  What LTO does is inline `pystate.c`'s *other* entry points that touch
the thread-local directly -- readers such as `PyThreadState_Get()`
(`current_fast_get()`) and writers such as `_PyThreadState_Attach()` /
`_PyThreadState_Detach()` / `PyEval_SaveThread()` (`current_fast_set()` /
`current_fast_clear()`) -- into ~400 functions across the interpreter,
`_PyEval_EvalFrameDefault` among them.  Each then holds a cacheable address of
the slot.  For example, the LTO build's `time_sleep` computes the slot's address
once and, inside its EINTR retry loop -- after `_PyErr_CheckSignals()`, which can
run Python and so park and migrate the fiber -- writes NULL through it (an
inlined detach): the use-after-free this patch exists to prevent, with no build
error and no test failure.  So
**never use `--with-lto`** (or `-flto` in CFLAGS) with these patches.
`--enable-optimizations` (PGO) does no cross-TU inlining and keeps the reads
confined to `pystate.c`; the cross-hub migration, parked-frame GC and swarm
scheduler tests pass on such a build.  CI builds without either
(`tools/ci/lib.sh` `rl_reject_lto` refuses both there: CI gains nothing from
PGO, and refusing it keeps the CI interpreter identical to the one the tests
were measured on).  Check any build with the script above: it attributes every
reader to its object file through the debug map (`nm -pa`) and fails on any
reader outside `pystate.o` -- which every LTO build gets, since LTO places even
pystate.c's functions in ThinLTO partition objects.  A stripped binary has no
debug map, and the script falls back to a count (more than a few dozen readers
is unsafe).

An earlier revision of this section said LTO "inlines
`_PyThreadState_GetCurrent()` back into its callers"; the measurement above shows
that it does not, and that the real path is the other `pystate.c` accessors.
(An alternative "inline form" of exec-home -- a volatile asm TLS read in
`_PyThreadState_GET()` and `current_fast_get()` instead of an out-of-line call --
is safe under LTO by construction and measured cheaper; it lives on the upstream
draft runloom PR #10 and is not what this directory ships.)

**⚠ Rebuild everything.** `_Py_ThreadId()` is inlined into `Py_INCREF`/`Py_DECREF`
through the *public* `refcount.h`, so the fix only reaches code compiled against
the patched headers. Every extension module in a migrating process must be rebuilt
— a prebuilt wheel keeps its cacheable reads.

**Known gaps:** on MSVC the thread-id reads are intrinsics (`__readgsqword`,
`__getReg`), not asm, and are left untouched — stackweave does not target
Windows. Two other thread-locals are in the same class but not covered because
no fiber path reads them across a park: `pkgcontext` (`Python/import.c`) and
mimalloc's `_mi_heap_default` (object allocation reaches the heap through the
tstate — see alloc-home — not through it).

### A dealloc that runs on the far side of a migration (`memoryobject.c`)

exec-home also carries one hunk outside thread identity. Biased refcounting
sends a non-owner's last decref to the OS thread that owns the object (`ob_tid`,
set where it was allocated), and that thread's merge runs `tp_dealloc`. For a
fiber that has moved, the owner is the hub it *allocated* on — so the dealloc of
an object the fiber never shared runs **concurrently** with the fiber's own next
statements on its new hub. Anything a dealloc and its creator both touch must
then be thread-safe even in a single-fiber program.

CPython has an **open class** of buffer-export counters that are not: a plain
`++`/`--` in a `getbuffer`/`releasebuffer` pair, or in a view's
register/release. The hunks fix two instances: memoryview's and
`array.array`'s.

The memoryview instance: `_PyManagedBufferObject.exports` — every view of a
buffer bumps it on creation and drops it on release/dealloc — is a plain
`++`/`--` upstream (still on main). The per-view `PyMemoryViewObject.exports`
is updated atomically by `memory_getbuf`/`memory_releasebuf`, but 3.14.4's
`memoryview.hex()` and `memory_hash()` still bump it with a plain `++`/`--`, so
the 3.14 hunk also backports the 3.14 branch's `FT_ATOMIC_ADD_SSIZE` there
(3.15 already has it). A fiber that holds
`sl = view[0:]` across a park and drops it on another hub has the old hub run
`--exports` while it runs `exports++` for its next slice. A lost increment
releases the buffer under the live `view` (`ValueError: operation forbidden on
released memoryview object`, and the exporter is unpinned, free to resize or
free memory the view still points at); a lost decrement pins the exporter for
good. Refcounts stay exact throughout. It is an upstream bug too — plain threads
slicing one shared view on a **stock** 3.14.4t lose updates the same way —
migration just turns a shared-object race into a private-object one. The hunk
makes both counts atomic under the flag. Measured with two interpreters built
identically apart from it (64 ping-pong pairs, each slicing its own view across
a park, ~3000 OS-thread moves per run): without it 43–179 released views and
93–283 leaked counts per run at H=2/4/8/12; with it zero over 10 runs. Guard:
`tests/test_cross_hub_migration.py::test_memory_memoryview_slice_survives_a_migration`.
`memoryobject.c` is not installed, so the exec-home `object.h` hunk also
defines `_Py_MV_EXPORTS_ATOMIC` as an installed witness; on an interpreter
patched with an older copy the guard xfails with a "rebuild from src/patches/"
message instead of failing.

The array.array instance: `ob_exports` in `Modules/arraymodule.c` is bumped by
`array_buffer_getbuf` and dropped by `array_buffer_relbuf`, a plain `++`/`--`
with no critical section (still on main; python/cpython#157759, filed under
gh-154524, proposes the same fix). A fiber holding `memoryview(arr)` across
a park and dropping it on another hub has the old hub run the release while it
takes the next view. A lost decrement pins the array for good (`BufferError:
cannot resize an array that is exporting buffers`); a lost increment lets it
resize, and free its items, under a live view. The hunk makes the count atomic
under the flag; the three "is it exporting?" checks before a resize read it
with acquire, so the resize is ordered after the release it saw. It fixes lost
updates only: `array_buffer_getbuf` reads `ob_item` before it counts the
export, so a resize on another thread in between can still leave a new view on
freed memory. Closing that needs a critical section, which arraymodule.c does
not have; migration cannot reach it, since one fiber does both steps. Measured, 64 ping-pong pairs each holding a view of its own array
across a park, batches to ~3000 OS-thread moves per run at H=8: without it
186–261 of 2112–2688 arrays lost an update per run (149–215 pinned, 37–49
resizable under a live view); with it, none over 10 runs. Guard:
`tests/test_cross_hub_migration.py::test_memory_array_view_survives_a_migration`,
which xfails the same way on an interpreter without the installed
`_Py_ARRAY_EXPORTS_ATOMIC` witness.

Not audited beyond that: the rest of the class. bytearray is safe (its
`ob_exports` is under the object's critical section).

### A lock owned by the OS thread, waited for by the OS thread (`lock.c`, `parking_lot.c`, `bufferedio.c`, `semaphore.c`)

`_PyRecursiveMutex` -- `_thread.RLock`, and CPython's own import and warnings
locks -- records `PyThread_get_thread_ident_ex()` at acquire and compares the
current OS thread at every release and `_is_owned()`. That ties the lock to
the OS thread in two ways a fiber breaks: a fiber that parks inside the lock
and resumes on another hub cannot release it (`RuntimeError: cannot release
un-acquired lock`, and the lock stays held for good), and every fiber on one
hub shares its OS thread, so a fiber that takes a lock another fiber on its
hub holds re-enters it instead of waiting -- both run the critical section,
and the second's release takes a level off the first's. `logging.Handler`
holds one of these around `emit`.

The `lock.c` hunk keys the mutex on a FIBER's thread state instead:
`tstate->id`, unique for the interpreter's life (`PyThreadState_GetID`),
tagged odd so it never collides with an OS thread id (a `pthread_t` is an
aligned pointer, a Windows thread id a multiple of 4). A fiber's state is one
the runtime marked (`_PyThreadStateImpl._fiber`, which the `pycore_tstate.h`
hunk adds; stackweave marks every per-g state at spawn). Every other state --
a thread's own, a hub's, a C thread's per-callback PyGILState state -- keeps
the OS thread, so code that never runs a fiber behaves exactly as stock: a C
library's thread may still take an RLock in one callback and release it in
the next, on a fresh thread state. `_PyRecursiveMutex_SelfIdent()` is exported for
`_PyImport_ReInitLock`, which re-owns the import lock after a fork (upstream
writes the raw OS id there, which no longer matches: the child's first import
died with a fatal "unlocking a recursive mutex that is not owned"). Fibers of
a nested `run(1)` or Coro run on the outer fiber's state and keep sharing its
locks. Witnesses: `_Py_RMUTEX_TSTATE_IDENT` and `_Py_TSTATE_FIBER_FLAG`
(`stackweave_c.rlock_tstate_owned`).

Identity alone is not enough. A contended `_thread.lock` or `RLock` waits in
the parking lot, which sleeps the OS thread on a semaphore. Under M:N that
thread is a hub, and with as many waiters as hubs on a lock held across a park
nothing can run the holder: a hard hang the deadlock detector cannot see (the
hubs are busy, not idle). Measured: six fibers on four hubs each holding a
lock across twenty foreign-woken parks hung for good. So the `parking_lot.c`
hunk adds `_Py_fiber_park`, three hooks a fiber runtime installs once:
`prepare(addr, flags)` runs under the bucket lock before the entry is queued
and names the current fiber, or NULL to wait on the platform semaphore as
usual (stackweave names one only where parking is sound: on the fiber's own
coroutine, not a `_Coro` it resumed; with its own state attached, not inside
`_interpreters.exec`; outside object destruction); `wait()` parks it, and may
return `Py_PARK_INTR` for a wait flagged `_Py_PARK_INTR` (an interruptible
acquire) when a signal is pending -- the run(1) drain on the main thread
interrupts it so CPython runs the handler in the fiber, as for a stock
main-thread acquire; `wake()` wakes it from any thread, at most once per
wait.
The lot keeps its queue, handoff and timeout-vs-unpark bookkeeping; only the
sleep is delegated, and only for a wait flagged `_Py_PARK_FIBER`, which the
lock objects pass as `_PY_LOCK_FIBER` from `acquire()` and RLock's
`_acquire_restore` (`_threadmodule.c` hunk), and `_PyRecursiveMutex_Lock`
passes for CPython's own recursive mutexes (`lock.c` hunk), which are held
across Python code by design: `importlib` runs every `find_spec()` under the
import lock, and `do_warn` runs `showwarning()` under the warnings lock. Every
other `PyMutex` wait, such as a critical section's, blocks the thread as
before. stackweave's `runloom_fiberlock.c` installs them at import and parks
through `runloom_park_generic` (foreign-wakeable), so the hub runs other
fibers and the same six-fiber run finishes in 0.1 s. With identity only, a
`showwarning` or a meta-path finder that parked while a fiber on every hub
warned or imported hung the process: each waiter blocked its hub, and the
holder had nowhere to resume. `prepare()` names a fiber only while that
fiber's own thread state is attached: a fiber inside a subinterpreter call
(`_interpreters.exec`) waits with the subinterpreter's state attached, and
parking there handed the hub or the `run(1)` drain a foreign state (`run(1)`
crashed), so such a wait blocks the thread. The uncontended path gains one
flag test. `_testinternalcapi/test_lock.c`'s whitebox owner assert follows
the identity. Witness: `_Py_FIBER_PARK_HOOKS` (`stackweave_c.fiber_lock_hooks`;
`fiber_locks_installed` says the hooks are in -- nothing at run time
leaves them out). Guards: `tests/test_fiber_locks.py`,
`tests/test_cross_hub_migration.py -k stock_rlock`, and the warnings and
import tests in `tests/test_cpython_thread_keyed_locks.py`
(`stats()["fiber_lock_parks"]`).

Two more locks have the same two problems and take the same identity
(`_PyRecursiveMutex_SelfIdent()`):

- `_io.Buffered*` (`bufferedio.c`) holds its lock across the raw stream's
  `readinto()`/`write()`, which is Python code when the raw stream is
  (`socket.makefile()`'s `SocketIO`, gzip/bz2/lzma readers, any `RawIOBase`
  subclass), and records `owner = PyThread_get_thread_ident()` to report a
  reentrant call. A second fiber on the holder's hub, while the holder was
  parked or preempted inside the raw stream, got `RuntimeError: reentrant
  call inside <_io.BufferedReader>`; one on another hub blocked its hub's
  thread. The hunk records a fiber's thread state as the owner (any other
  state, the OS thread, as stock) and makes the lock a
  `PyMutex` (a platform lock in 3.14, a `PyMutex *` behind the same API in
  3.15), whose contended wait passes `_PY_LOCK_FIBER`. Witness grep:
  `BUFFERED_SELF`. Guard:
  `tests/test_cpython_thread_keyed_locks.py -k shared_buffered`.
- `multiprocessing`'s recursive SemLock (`mp.RLock`, `mp.Value`'s lock;
  `semaphore.c`) decides "mine" by `last_tid == PyThread_get_thread_ident()`.
  After a migration the holder's re-acquire (`with v.get_lock(): ...;
  v.value += 1`) waited on itself in `sem_wait`, and a fiber on the holder's
  hub counted as the owner. The hunk keys `last_tid` on a fiber's thread
  state (any other state: the OS thread, as stock).
  Its wait is a cross-process semaphore, outside the parking lot, so a
  contended SemLock still blocks the hub's thread. Witness grep:
  `SEMLOCK_SELF`. Guard: `tests/test_cpython_thread_keyed_locks.py -k mp_value`.

All three take the identity only for a fiber's thread state, so a plain
thread's behaviour is unchanged: a C thread whose `PyGILState_Ensure()` state
is torn down between acquiring and releasing one of these locks (one
`Ensure`/`Release` pair per callback) still owns it the second time, keyed on
its OS thread as upstream. Guard:
`tests/test_fiber_locks.py::test_a_plain_thread_rlock_across_pygilstate_callbacks_is_stock`.

### A frame follows its thread state's bytecode copy (`ceval_macros.h`, `ceval.c`; own flag `Py_TSTATE_TLBC_FOLLOW`)

Free-threaded CPython gives every thread state its own copy of each code
object's bytecode (thread-local bytecode, TLBC), because the specializer
rewrites instructions and inline caches without atomics. stackweave gives every
fiber a thread state, so every live fiber got its own copy of every function it
ran (2000 fibers that ran one 144 KB function: 663 MB, against 281 MB with
`PYTHON_TLBC=0`). stackweave now lends a resumed fiber its hub's TLBC index
(`runloom_iframe_tlbc_lend`: a swap of the two states' indices for the length
of the resume), so live fibers share one copy per hub. A frame keeps executing
the copy it started on until its next `RESUME`, though, and a fiber parks inside
a call, mid-instruction: after a migration the frame would run hub A's copy on
hub B's thread while A runs it too, the race TLBC exists to prevent (with
lending forced on without this hunk, `tools/probes/ft_locks/p23_tlbc_shared_stress.py`
segfaults in the eval loop 3 runs in 3).

The hunk re-points a frame at its thread state's copy before each instruction
starts: `DISPATCH()` compares the frame's index with the thread state's and
branches to one shared slow path (`_PyEval_TLBCFollow`) on a mismatch, and
`DISPATCH_SAME_OPARG()`, `JUMP_TO_PREDICTED` and `PAUSE_ADAPTIVE_COUNTER` (which
the instrumented `LINE`/`INSTRUCTION` paths reach before running a cached
opcode) do the same. Only the instruction whose call parked finishes on the old
copy; in 3.14.4 that tail reads no inline cache but a function version it checks
against the callee and writes only the atomic branch-history bits, except when
the park happened inside the specializer itself (a finalizer run by one of its
decrefs). The 3.15 hunk is the same, plus `DISPATCH_NON_TRACING()`.

It has its own flag, `-DPy_TSTATE_TLBC_FOLLOW` (meaningful only with
`Py_TSTATE_EXEC_HOME`), which `tools/ci/versions.env` sets with the other two
(`RL_CI_FEATURE_DEFINES`). `_PyEval_TLBCFollow` exists only under it;
stackweave lends only when `dlsym` finds it (`stackweave_c.tlbc_follow`,
`stackweave_c.tlbc_shared`), so an interpreter built without the flag keeps
per-fiber copies and stays sound. Build without it with
`RL_CI_FEATURE_DEFINES="Py_TSTATE_ALLOC_HOME Py_TSTATE_EXEC_HOME"
tools/ci/build_patched_cpython.sh <version>`.

Cost: a compare and a branch per instruction, about +4% on interpreter-bound
microbenchmarks against the same build without the flag (3.14.4, arm64
Darwin, no LTO/PGO; per-benchmark numbers in docs/dev/FT_CPYTHON_LOCK_GAPS.md
#15). Gain: p21 at 2000 fibers 158 MB instead of 663; a
10k-fiber pure-Python handler (urllib.parse, http.cookies, dataclasses, ...)
135 KB per fiber instead of 160. Guards: `tests/test_tlbc_hub_copies.py`,
`tests/test_cpython_thread_keyed_locks.py::test_cost_live_fibers_share_bytecode_within_1_5x_of_tlbc_off`
(a known gap on an interpreter built without the flag).

### Fiber preemption at the eval breaker (`ceval_gil.c`)

The last exec-home hunk is not a migration fix but a scheduler hook. sysmon
preempts a fiber that holds its hub past the time slice by switching it out
from inside the eval loop, and CPython offered two ways in, each with a cost:

- **A PEP 523 eval-frame function** runs at every frame entry, but its mere
  presence (`interp->eval_frame != NULL`) makes every specialized
  Python-to-Python `CALL` deoptimize, process-wide: each call re-enters
  `_PyEval_EvalFrameDefault` on the C stack. Measured (3.14.4t, arm64 Darwin,
  no LTO/PGO): `fib(25)` 4.5 → 8.1 ms, in fibers and in plain threads alike once
  installed, and recursion in a fiber's 512 KB stack stopped near **180
  frames** (~2 KB of C stack per call) instead of at the recursion limit.
- **A pending call** costs nothing until used, but a callback that switches the
  fiber out stays inside `make_pending_calls()`, which holds the interpreter's
  pending-call slot (`pending->handling_thread`) until the fiber resumes, so no
  other thread's fiber can be preempted meanwhile.

The hunk adds `_PyEval_SetFiberBreakerHook(hook)`, whose hook the end of
`_Py_HandlePending()` calls once every slot it takes is released, and
`_PyEval_RequestFiberBreak(tstate)`, which raises one thread's eval breaker so
the hook runs at its next check: a function entry (`RESUME`) or a loop
back-edge, single-frame loops included. stackweave registers the hook in place
of the eval-frame function, and sysmon calls the request on the tstate it reads
under its hazard pointer. The request borrows `_PY_ASYNC_EXCEPTION_BIT` -- all
eight event bits are taken -- because it is per-thread and its handler does
nothing when no `async_exc` is queued. `_PY_CALLS_TO_DO_BIT` would not do: on a
free-threaded build `make_pending_calls()` clears it on **every** thread state,
dropping other threads' requests at a cost linear in thread states, and under
M:N every fiber has one.

With the hook, `fib(25)` in a fiber runs in 4.6 ms and a fiber recurses to the
recursion limit; three single-frame loops pinned to one hub all take turns.
Guard: `tests/test_preempt_breaker.py` (it asserts
`stats()["preempt_via_breaker"]`) and the `single_frame` case of
`tests/test_sched_fairness.py::test_preemption_takes_turns_between_fibers_on_one_hub`.
The installed witness is `_Py_FIBER_BREAKER_HOOK` in `Include/cpython/ceval.h`;
an extension built against an interpreter without it keeps the eval-frame
function (`stackweave_c.fiber_breaker_hook == 0`), and those guards xfail
asking for a rebuild.

## Using it

Migration is **always on** under M:N (`run(n > 1)`); there is no switch. What it
needs is an interpreter built with **both** patches:

1. **Build CPython with both patches.** `tools/ci/build_patched_cpython.sh` does
   the whole thing — fetch the pinned release, verify its sha256, apply the pair
   at zero fuzz, configure, build, install, and assert both halves report present:
   ```sh
   tools/ci/build_patched_cpython.sh 3.14.4   # pins live in tools/ci/versions.env
   ```
   By hand, for 3.14 (use the `315` files on 3.15 — they are not interchangeable):
   ```sh
   cd cpython
   patch -p1 -F0 < .../patches/cpython314t-tstate-alloc-home.patch
   patch -p1 -F0 < .../patches/cpython314t-tstate-exec-home.patch
   ./configure --disable-gil CPPFLAGS="-DPy_TSTATE_ALLOC_HOME -DPy_TSTATE_EXEC_HOME -DPy_TSTATE_TLBC_FOLLOW"
   # ALSO put the #defines in pyconfig.h -- CPPFLAGS covers only the CPython
   # build, while extension modules include the INSTALLED pyconfig.h.  alloc-home
   # adds a field to _PyThreadStateImpl, so a mismatch shifts struct offsets
   # silently rather than failing to link.  (Drop Py_TSTATE_TLBC_FOLLOW from
   # both lines to keep one bytecode copy per fiber and save ~4%.)
   printf '#define Py_TSTATE_ALLOC_HOME 1\n#define Py_TSTATE_EXEC_HOME 1\n#define Py_TSTATE_TLBC_FOLLOW 1\n' >> pyconfig.h
   make && make install
   ```
   Use `-F0` and treat any reject as a hard stop. Both flags are armed in
   `pyconfig.h` independently of whether the hunks landed, so a half-patched tree
   **advertises** the features while lacking them. Verify with the compile-time
   witnesses before building — this is what the CI's `rl_verify_witnesses` checks:
   ```sh
   grep -q _Py_TID_ASM Include/object.h                            # exec-home
   grep -q _Py_MV_EXPORTS_ATOMIC Include/object.h                  # exec-home
   grep -q MBUF_EXPORTS_INC Objects/memoryobject.c                 # exec-home
   grep -q _Py_ARRAY_EXPORTS_ATOMIC Include/object.h               # exec-home
   grep -q ARRAY_EXPORTS_INC Modules/arraymodule.c                 # exec-home
   grep -q _Py_RMUTEX_TSTATE_IDENT Include/object.h               # exec-home
   grep -q recursive_mutex_self Python/lock.c                      # exec-home
   grep -q _Py_FIBER_PARK_HOOKS Include/object.h                 # exec-home
   grep -q _Py_TSTATE_FIBER_FLAG Include/internal/pycore_tstate.h  # exec-home
   grep -q _Py_fiber_park Python/parking_lot.c                     # exec-home
   grep -q BUFFERED_SELF Modules/_io/bufferedio.c                  # exec-home
   grep -q SEMLOCK_SELF Modules/_multiprocessing/semaphore.c       # exec-home
   grep -q _PyEval_TLBCFollow Python/ceval.c                       # exec-home, TLBC follow
   grep -q _PyThreadStateImpl_AllocHome Include/internal/pycore_tstate.h  # alloc-home
   ```

   Then build stackweave against that interpreter (`python setup.py build_ext --inplace`),
   **and rebuild every other extension module you load** — `_Py_ThreadId()` inlines
   into `Py_INCREF`/`Py_DECREF` via the public `refcount.h`, so a wheel built against
   unpatched headers keeps the cacheable reads.

   The same stackweave source builds against **stock** CPython too — both features
   compile out — but nothing stops an M:N run there, and it migrates fibers
   without either fix.

2. **Run it.** `stackweave.run(n_hubs, main)` with `n_hubs > 1` migrates woken
   fibers to any idle hub.
