# CPython locks, conditions and thread identity under fibers

Status: investigation, 2026-11-08, branch `investigate/ft-cpython-locks` off
main 8ad634c1. Interpreter: `~/.pyenv/versions/3.14.4t-mig` (the committed
alloc-home + exec-home patches; NOT the in-progress `fix/rlock-fiber-identity`
hunks), macOS arm64, sqlite 3.54 (system, multi-thread mode). Nothing here is
fixed. Linux was not run.

Probes: `tools/probes/ft_locks/`, run as `./run.sh pNN_*.py args`; each prints
PASS or FAIL (or hangs into its watchdog). Every "confirmed" below was
reproduced with its probe; the numbers are from this box.

Strict known-gap tests: `tests/test_cpython_thread_keyed_locks.py`, one per
stackweave-visible finding (1-15 and 17; not 16, which is CPython's alone, nor
18, which is documented design). Each prints REACHED once its trigger
happened, so a scenario that misses its trigger fails outright instead of
counting as the gap.

## How a fiber looks to CPython

A fiber has its own `PyThreadState`, attached on the hub thread while it runs
and detached with `PyEval_SaveThread` when it parks, so critical sections and
QSBR behave as for a blocked thread. Anything keyed on the OS thread does not
follow it: `PyThread_get_thread_ident`, `pthread_self`, `_Py_ThreadId`
(`ob_tid`), `_PyRecursiveMutex` owners, pthread / `os_unfair_lock` ownership,
thread-locals. At any Python-level call a fiber can park and resume on another
hub; several live fibers share each hub's OS thread; a preempted fiber stays on
its hub's ready ring, which nothing steals.

Hazard classes: **A** identity recorded and compared later; **B** a lock that
is not a critical section held while C calls into Python; **C** the hub's OS
thread blocks on something only a fiber can signal; **D** thread-locals across
a callback; **E** protocols assuming one tstate per OS thread.

The single most productive pattern: **an OS-thread-owned lock held across a
Python callback**. If the fiber migrates inside the callback, the unlock runs
on the wrong OS thread: CPython's `_PyRecursiveMutex` refuses and the lock
leaks for good (warnings, import), an OS lock aborts (sqlite on macOS), a
semaphore's owner check self-deadlocks (multiprocessing). If it does not
migrate but parks or is preempted, a same-hub contender either re-enters or
blocks the only OS thread that could run the holder.

## Confirmed: crash or memory safety

### 1. sqlite3 callback that parks and migrates aborts the process (B)

**Status: FIXED** (commit "sched: hold a fiber on its hub while a foreign C
caller is on its stack"). While a fiber's thread state has a nested
`PyGILState_Ensure` (`gilstate_counter > 1`; `PyThreadState_New` gives a
per-g state 1), the hub that switches it out holds it there through
the pin machinery (`runloom_mn_hold_for_c_caller`: `cpin_hub1` overrides the
user's pin in `pin_hub1`), so the callback resumes on the OS thread the
library locked from. A `G.pin()` made meanwhile takes effect once the callback
has returned. That covers every PEP 311 caller: sqlite3's callbacks, ctypes,
cffi, Cython `with gil`, pybind11 (which bumps the counter itself), `_ssl`'s
SNI / msg / keylog callbacks. Not covered: callers that re-attach a saved
thread state without the counter (`_tkinter`, `_ssl`'s password callback,
`PyThreadState_Swap`). Not changed: a held fiber still parks, so another fiber
of its hub can use the same connection while the statement is suspended
(sqlite allows SQL from inside a user function), and it waits behind a busy
hub like any pinned fiber (#14). A callback stays preemptible, by decision: a
preempted fiber keeps to its hub's ready ring anyway, while other fibers of
the hub run. Probes p10 / p11 pass. Guards:
`test_identity_sqlite_callback_keeps_its_os_thread_across_a_park`,
`tests/test_cross_hub_migration.py::test_identity_c_callback_keeps_its_os_thread_until_it_returns`
(`stats()["mn_c_caller_holds"]`).

`p11_sqlite_udf_min.py serialized`: 3/3 SIGKILL, no Python traceback.
User functions, aggregates, window functions, collations, authorizer,
progress and trace callbacks run inside `sqlite3_step` (or prepare), which
holds the connection mutex in serialized mode. The callback parks, resumes on
another hub, and `sqlite3_step` releases the mutex from the wrong OS thread:
macOS aborts in `_os_unfair_lock_unowned_abort`. `check_same_thread` (default
True) does not catch it: it runs when `execute` starts, before the migration.

- macOS's system libsqlite3 is multi-thread mode (`sqlite3.threadsafety == 1`,
  no connection mutex); the probe switches to serialized with
  `sqlite3_config(SQLITE_CONFIG_SERIALIZED)` through ctypes. Debian/Ubuntu,
  Homebrew and python.org builds are serialized (`threadsafety == 3`). On
  glibc a non-owner unlock of the recursive mutex returns EPERM, which sqlite
  ignores, so expect the connection mutex to stay held by the first hub's
  thread and other hubs to hang on it (not run).
- Same shape in other C libraries that hold an owner-checked lock across a
  Python callback: `_tkinter` (Tcl is bound to its thread), `_ssl`'s
  `load_cert_chain` password callback (holds a PyMutex), ctypes/cffi callbacks
  into user libraries.
- Fix (above): such callbacks enter through `PyGILState_Ensure`, so a nested
  `gilstate_counter` on the fiber's tstate at park time marks "foreign C frame
  on the stack": the fiber is held on its hub until the matching Release.

### 2. A `__del__` that parks mid merge-drain lets the rest merge on the wrong thread (B)

`p12_brc_wrong_thread_merge.py 4 300`: FAIL, 3-4 objects per run merged and
freed on a thread that does not own them (gc disabled, never merged before).
`_Py_brc_merge_refcounts` moves the bucket queue into the tstate's
`local_objects_to_merge` and pops it, deallocating each object that reaches
zero. If a `__del__` there parks, the hub runs
`runloom_iframe_brc_release` (`src/runloom_c/runloom_iframe.c:366`), which
drains only when the SHARED queue `objects_to_merge` is non-empty, not when
`local_objects_to_merge` still holds objects. The fiber resumes on another
hub and finishes the loop there: `_Py_ExplicitMergeRefcount` reads and zeroes
`ob_ref_local` of objects whose owner hub may be bumping that field
non-atomically right then. A lost increment frees a live object. The
preemption gate (`runloom_tstate_in_destruction`) covers this window for
preemption only. Rare in the probe because a nested merge inside the parking
`__del__` usually empties the stack first. Fix: also drain in release when
`local_objects_to_merge.head != NULL` (on the hub, the right thread).

**Status: FIXED on fix/ft-brc-finalizers.** `runloom_iframe_brc_release`
counts the local stack as pending, so the drain it already runs (fiber at the
head of the bucket, hub's C-stack limits lent) empties both stacks on the
owner; `stats()["brc_release_local_drains"]` counts the releases that found
one part-drained. The fiber's suspended loop then finds its stack empty and
returns: it holds no pointer into the stack across `_Py_Dealloc`
(`_PyObjectStack_Pop` re-reads `head` and frees each chunk as it empties it),
and plain threads already get the same thing from a nested merge in a
`__del__` or from a GC merging a thread blocked in one.

The cost: the rest of that batch is now deallocated on the hub after the fiber
has switched out, so any later `__del__` in it runs with no current fiber. A
patched lock or `stackweave.sleep` there takes the foreign-thread path and
blocks the hub's OS thread; if a fiber on the same hub holds that lock, the
hub waits on itself. That is finding #10's class (finalizers with no fiber),
and the trade is deliberate: merging on the right thread beats a cooperative
finalizer that merges on the wrong one. Closing it is #10's work.

The original probe did not show this mechanism. Its count came from its
oracle: `__del__` starts with an eval-breaker check (RESUME), where a nested
merge can run an A whose `__del__` migrates the fiber. B's `__del__` body then
runs on another thread although B was merged on its owner. With debug
counters, no queue was ever carried across a hub change and the local stack
was never non-empty at a park; force_migrate's thread start drops
producer-owned objects (the Condition waiter lock), which re-sets the merge
bit, so a nested merge always emptied the stack first. The probe and
`test_memory_queued_objects_merge_on_their_owner_thread` now trigger it
deterministically: a plain OS thread drops B, B, B, A while the producer fiber
spins in C on its hub (`deque(islice(iter(poll, 1), N), maxlen=0)` until the
dropper sets a flag): no bytecode runs, so no eval-breaker check merges early,
and the producer's state stays attached at the head of the bucket, so one
merge batch holds all four with A on top. A's `__del__` then parks with no
Python before the park, pinned to resume on another hub. Blocking does not
work. With the fiber-lock hooks a fiber's contended acquire parks it, the
drops queue to the hub's own state and A's `__del__` runs with no fiber; the
lock version hung that way on Linux 3.14 CI. `os.read` detaches the state, and
from 3.15.0rc3 (gh-157838) the dropper merges a detached owner's queue itself,
on its own thread; that version failed on 3.15 CI. The oracle counts such a
merge-on-behalf apart (`b_on_dropper`), as a failed setup rather than this bug,
and a lost handshake fails the scenario instead of hanging it. Before the fix: 300/300 B's in the test and 900/900 in
`p12 4 300` merged off their owner. After: 0, with one local drain per round.

## Confirmed: a lock leaks to the old OS thread, then everything hangs

### 3. warnings lock (B, process-wide hang)

`WMODE=showwarning p16_warnings_lock.py 4`: watchdog. C `do_warn`
(`Python/_warnings.c:1170`) holds the interpreter's warnings
`_PyRecursiveMutex` across `warn_explicit` -> `showwarning` (and message
`__str__`, category construction, linecache). A `showwarning` that does I/O
(e.g. `logging.captureWarnings(True)` with a network handler) parks and
migrates; the unlock on the new thread fails and `do_warn` ignores the return
value, so the lock stays owned by the old hub's OS thread. The next
`warnings.warn` on any other hub -- even for an ignored category -- blocks
that hub forever; so do `catch_warnings` and `simplefilter`.
The `os.stat` that `linecache` does inside the lock (offloaded by
monkey.patch) did not migrate in 6 tries (`WMODE=stat`), so that trigger is
possible but not shown.

### 4. Global import lock (B, process-wide hang; known gap, new trigger and outcome)

`p17_import_lock_finder.py`: the first import raises
`RuntimeError('not holding the import lock')`, then imports on every hub hang.
`importlib._bootstrap._find_spec` runs every `finder.find_spec()` under
`_ImportLockContext` (the C IMPORT_LOCK, `_PyRecursiveMutex`), not just
`_get_module_lock`. A meta-path finder that does I/O (setuptools' editable
finder calls `Path.exists()` -> patched `os.stat`) parks and migrates inside
it. Also reached by `_ModuleLock` weakref callbacks and `os.fork()`.

### 5. `multiprocessing.Value`'s lock self-deadlocks after a migration (A)

`p14_mp_semlock.py [patch]`: watchdog, with or without monkey.patch.
`with v.get_lock(): <park, migrate>; v.value += 1` -- the property re-acquires
the same recursive SemLock, whose `ISMINE` compares the OS thread
(`Modules/_multiprocessing/semaphore.c:49`), so the owner now looks like a
stranger and waits on itself in `sem_wait`, blocking its hub's OS thread.
`mp.Condition` and `mp.RLock` are the same kind.

**Status: FIXED under monkey.patch;
the stock case stays a migration gap.** Two holes let a fiber reach the C
recursive object: `SemLock.__enter__`/`__exit__` called it directly (so
`with lock:` was a blocking `sem_wait` and a hold the cooperative release never
saw), and the cooperative path still used it for the outermost acquire and the
final release (so the release after a migration raised "not owned by thread").
Now `_CoRecursiveSemLock` replaces `SemLock._semlock` for recursive locks: owner
and depth are per fiber, and the cross-process gate is a SEMAPHORE-kind twin of
the same kernel semaphore (`_rebuild` with the name, or a shared handle kept
for the process's life when the name is already unlinked), which has no owner
check. `_is_mine()`/`_count()` answer from the fiber state, so
`mp.Condition.wait` releases a nested hold. `__enter__`/`__exit__` route
through the instance's acquire/release. The SemLock patch also used to apply
only when multiprocessing was imported before `patch()`; a `sys.meta_path`
hook now patches `multiprocessing.synchronize` as it loads. Without
monkey.patch, nothing in stackweave can intercept the C object.

## Confirmed: a hub stalls or deadlocks (no migration needed)

### 6. A preempted lock holder deadlocks its hub (C)

`p09_preempt_holding_lock.py stock|strptime`: watchdog; `patched` PASS;
`rlock`: re-enters (B waited 0.0 s, the known RLock gap).
Fiber A runs CPU-bound Python inside `with lock:` (no park at all), is
preempted, and waits on its hub's ready ring. B on the same hub contends,
blocks the OS thread in the stock acquire, and A never runs again. Stdlib
locks do it implicitly: `time.strptime()` while another fiber on the hub is
preempted inside `_strptime._cache_lock`. `_fixup_preimported_stdlib_locks`
only replaces `tempfile._once_lock`; logging handlers' RLocks created before
`patch()` and `logging._lock` are the same. The in-progress parking-lot hook
(`_PY_LOCK_FIBER`) fixes `_thread` locks only; the warnings and import
`_PyRecursiveMutex` waits (3, 4) need it too.

### 7. sqlite busy timeout blocks the hub for its full length (C)

**Status: FIXED under monkey.patch** (commit "monkey: wait for a locked
sqlite database outside sqlite3_step"). A new `sqlite` category
(`src/stackweave/monkey/sqlite.py`, `src/runloom_c/module_sqlite.c.inc`)
replaces each connection's busy handler with one that gives up at once where
sqlite would have waited, but only for the calls sqlite says may be retried
after SQLITE_BUSY, made on a fiber: a statement (`Cursor.execute` /
`Connection.execute`) run while the connection is in autocommit, and a commit
(`commit()`, `with con:`, a COMMIT statement). The statement is reset, the
transaction Python's implicit BEGIN opened for it (if any) is rolled back, and
the call sleeps cooperatively on sqlite's own backoff up to the connection's
timeout, then runs again. Waiting outside `sqlite3_step`, not parking inside
the handler: inside it the statement is suspended and sqlite's busy handler is
not reentrant, while serialized mode's recursive mutex would let another fiber
of the hub into the same connection. For the same reason a call made while a
foreign C caller is on the stack (SQL run from another connection's user
function, inside that one's `sqlite3_step`) is never armed. A BUSY sqlite
returns without consulting the handler (the read-to-write deadlock) is raised
unchanged. An armed statement whose cache spill asks for the exclusive lock
gets no wait for it: the spill fails and sqlite grows the page cache instead.
A connection with `check_same_thread=True` sleeps held on its hub
(`runloom_mn_hold_here`), as sqlite's own wait never left the OS thread.
Unchanged, so still blocking: unpatched sqlite (any blocking C call blocks its
hub), a statement inside an open transaction (retrying it there is not
sound), calls under a foreign C caller, `executemany` / `executescript` /
fetches / `backup` / blobs (no safe whole-call retry), connections opened
before `patch()` or through `sqlite3.Connection(...)`, and a connection after
`PRAGMA busy_timeout`. The
`sqlite3 *` is read from the Connection object (first field, checked on 3.14
and 3.15, cross-checked against autocommit and total changes), because
`sqlite3_auto_extension` returns SQLITE_MISUSE in macOS's system library.
`p13_sqlite_busy.py 2 patch`: PASS (B waits ~57 ms). Guards:
`test_preempt_sqlite_writer_waits_for_a_same_hub_transaction` (now patches),
`tests/test_monkey_sqlite.py`.

`p13_sqlite_busy.py 2`: FAIL, writer waited 2.095 s then
`OperationalError('database is locked')`; threads succeed in ~50 ms. One
connection per fiber on one database file: fiber A holds the write lock across
a park (implicit BEGIN, then `stackweave.sleep`), fiber B on the same hub
writes. sqlite's busy handler sleeps inside `sqlite3_step` on the hub's OS
thread, and A's wake-up timer lives on that hub's heap, so A cannot commit
until B gives up. The default timeout is 5 s. `Connection.backup` retries the
same way.

## Confirmed: wrong results or spurious errors

### 8. Shared buffered stream: false "reentrant call" on one hub (A)

`p04_buffered_reentrant.py {park-read|preempt-read|preempt-write} 2 same`:
FAIL every time; the `diff` (two hubs) variants PASS.
`_io.Buffered*` holds `self->lock` with `owner = PyThread_get_thread_ident()`
across the raw stream's `readinto`/`write` (`Modules/_io/bufferedio.c:294`).
When the raw stream is Python code (`socket.SocketIO` from `makefile()`, gzip /
bz2 / lzma readers, any `RawIOBase` subclass), the holder can park or be
preempted inside it, and a second fiber on the same hub gets
`RuntimeError: reentrant call inside <_io.BufferedReader ...>`. A fiber on
another hub blocks its hub's OS thread until the holder is done.

### 9. `PyThreadState_SetAsyncExc` interrupts the wrong fiber (E)

`p18_setasyncexc.py`: FAIL, the bystander on the same hub got the exception.
The stopit / func_timeout idiom records `threading.get_ident()` and later calls
`PyThreadState_SetAsyncExc(ident)`, which picks the newest tstate with that
`thread_id` (`Python/pystate.c:2519`): whatever is running on the hub, once
the target has parked.

### 10. Finalizers mostly run with no fiber, where cooperative ops fail (E)

`p07_del_parks.py {sleep|chan} 4 2000`. Objects allocated on hub 0 and dropped
on hub 1 are destroyed by hub 0's merge drain: 1583 of 2000 `__del__` ran with
`current_g() is None` on the hub's own tstate
(`runloom_iframe_service_merge_queue` after a resume, or the release drain).
There `stackweave.sleep` silently blocks the hub's OS thread (1.17 s in total)
and a channel op raises `RuntimeError: ... no goroutine is running`; the
fibers spawned to answer those ops are left blocked and reported at exit. So a
finalizer that logs, returns a connection to a pool, or does patched I/O
behaves differently depending on which hub dropped the last reference.

**Status: NOT FIXED; design below, xfail kept
(`test_memory_finalizer_can_use_a_channel_whichever_hub_drops_it`).** After
the #2 fix, `p07 chan 4 2000` still runs 1951 of 2000 finalizers with no fiber
(and 23 releases found a part-drained local stack). The fix needs every merge
that a hub runs today outside a fiber to run inside one. That touches the
release-drain and C-stack-limit invariants and their guards, the hub's pick
step and post-resume state machine, pending accounting (which the deadlock
census reads), fini and the spawn path. That is too much to land blind on a
shared box, without the full suite and the formal lanes. Decisions for the
owner are marked **(decide)**.

Design: a per-hub **reaper**, a pinned fiber that runs this hub's merges.

1. *Where merges run today outside a fiber*: (a) `service_merge_queue(hub_ts)`
   at the loop top and after every resume (the hub's own state heads its
   bucket between resumes, so drops queue to it), and (b) the release drain
   (a parked fiber's shared queue, plus since #2 its local stack, on the hub
   stack with `tls_current_g` NULL). A fiber's own eval-breaker merge already
   runs in a fiber and stays as it is.
2. *Hand off instead of merge*. Both sites move queues instead of running
   them. Under the bucket mutex, they splice the source's `objects_to_merge`
   (and, at release, its `local_objects_to_merge`, which only its own state
   touches) onto the reaper's `objects_to_merge`. Then they set the reaper's
   merge bit and kick it. This is pure pointer work: no object code runs on
   the hub thread, so (b) no longer needs the hub's C-stack limits lent, and
   the "hub to the head only once the fiber's queue is empty" rule holds
   trivially. The splice is legal because the reaper is created on the hub's
   thread and pinned to it: its `brc.tid` is the hub's, it sits in the same
   bucket (same mutex), and `_Py_ExplicitMergeRefcount` will run on the owner.
   `_PyObjectStack_Merge` is not exported, so it needs a local copy: link the
   source's bottom chunk to the destination's head. Clear the source's merge
   bit before the splice, so a push that races in afterwards re-sets it.
3. *The reaper*. A fiber whose callable is a builtin running a C loop.
   Python-callable, not `runloom_mn_fiber_c`, so `runloom_g_entry` arms its
   root frame, datastack and stack limits, since it runs Python finalizers.
   The loop: `service_merge_queue(own ts)` while the merge bit is set, then
   idle. Spawn it through the normal slab/`runloom_greg_link` path (frames
   anchor). Pin it to its hub, leave it out of the fiber limit, and create it
   lazily on the hub thread the first time the hub has merge work.
4. *Idle and kick*. To idle, the reaper sets `g->reaper_idle` and does a plain
   `runloom_coro_yield` (after `runloom_pystate_snap`, as a yield does). In
   hub_main's yield branch, a reaper with that flag set goes to `h->reaper_idle_g`
   instead of `runloom_mn_requeue_local`. It stays RUNNING, owned by the hub
   with no queue ref, and only the hub thread touches the slot. A kick moves
   it from the slot to a priority slot that the pick step takes before
   anything else, so merges still run right after the resume that preceded
   them, as now. (On the ready ring it could wait out every preempted CPU-bound
   fiber there; that is the gap-#14 shape.)
5. *A finalizer that parks*. The reaper parks like any fiber. Its release
   must not touch its own queues: it is pinned, so its suspended merge loop
   resumes on the owner. It must still put the hub back at the head. The
   reaper is then not at the head, so no dropper (and no 3.15 merge-on-behalf)
   reaches its queues; only GC under stop-the-world does, and that is safe.
   Work that arrives while it is parked: **(decide)** either queue it behind
   that reaper (CPython's thread-blocked-in-`__del__` behaviour, but a stuck
   finalizer then stalls the hub's cross-thread frees with no bound), or detach
   it (it finishes its batch and exits) and start a new reaper for new work, up
   to a per-hub cap, past which hand-off waits.
6. *Pending and shutdown*. **(decide)** The proposal is that a reaper counts in
   its hub's `pending` from kick to idle, so `run()` does not return with
   finalizers in flight. A reaper parked forever then shows up in the deadlock
   census like any blocked fiber. An idle reaper is never queued, so the
   census, which reads the queues, sees no work. At hub exit the idle reaper
   is suspended in C with no Python frames; `runloom_mn_hub_drain_leftovers`
   decrefs it like a leftover queued g. Drops after `run()` returns are merged
   by the hub state's own `_Py_brc_remove_thread`, with no fiber, as today.
7. *Guards that change*. `brc_release_merges`, `_off_stack` and `_hub_first`
   (and their two tests) describe the hub-stack drain, which goes away;
   replace them with hand-off and reaper-run counters. The #2 guard's
   positive check becomes "a part-drained local stack was handed to the
   reaper". `runloom_tstate_in_destruction` still keeps preemption out of the
   reaper's merge loop.
8. *What users see*. Cross-hub finalizers run on a system fiber:
   `current_g()` is the reaper, and it appears in fiber dumps and live counts,
   which should filter it out. Objects that another thread drops are freed one
   switch later than now. The reaper's priority slot keeps that to
   microseconds, but tests that check a weakref right after a cross-hub drop
   need a full-suite run before merge.

### 11. asyncio debug mode raises after a migration (A)

`p22_asyncio_debug.py`: `RuntimeError: Non-thread-safe operation invoked on
an event loop other than the current one`. `BaseEventLoop._check_thread`
compares `get_ident()` (debug only: `-X dev`, `PYTHONASYNCIODEBUG`,
`IsolatedAsyncioTestCase`). A stock loop inside an M:N fiber parks in its
patched selector and resumes elsewhere. `stackweave.aio` (nested run(1)) does
not migrate.

**Status: FIXED under monkey.patch.** A
new monkey category, `asyncio` (`monkey/aioloop.py`), records the fiber that
starts a `BaseEventLoop` (`_run_forever_setup`) and makes `_check_thread`
compare fibers; a loop started off a fiber keeps the stock check, another
fiber is still refused, and `_thread_id` is left as stock sets it. Nothing else
in asyncio keys on the OS thread (the running loop and current task are per
tstate; `call_soon_threadsafe` checks nothing). Without monkey.patch the stock
check still fires.

### 12. A thread started from a fiber is a daemon, so exit drops its work (E)

`p20_thread_daemon.py`: `daemon: True`, and the thread's work never finished.
`Thread.__init__` copies `daemon` from `current_thread()`, the hub's
`_DummyThread` (daemon), so interpreter exit does not join it.

## Confirmed: in stackweave itself

### 13. Patched `os.read` / `os.write` leave the fd non-blocking

`p15_osread_nonblock.py`: `blocking(r)` True before, False after.
`monkey/osio.py` calls `os.set_blocking(fd, False)` on any pollable fd and
never restores it. O_NONBLOCK lives on the open file description, which dups,
`sys.std*` on a tty and child processes share: a later plain read there gets
EAGAIN (`BlockingIOError`, or `None` from a buffered reader, i.e. a short
read). Popen's own pipes survived because the subprocess patch covers them.

**Status: FIXED.** The patches no longer
touch the mode. An fd already non-blocking keeps the try/park loop; a blocking
socket uses `MSG_DONTWAIT` per call (recv everywhere; send off macOS, whose
send blocks with it); a blocking pipe or char device waits for a
zero-timeout, level-triggered probe (select, or poll above FD_SETSIZE) under a
per-inode stripe mutex and then makes the plain call, pipe writes in PIPE_BUF
pieces so each fits and the whole buffer is written; a tty write, and any fd
the probe or netpoll cannot watch, goes to the pool. Residual: a reader or
writer outside this process's fibers that drains or fills the pipe between
probe and call blocks the hub until the pipe has data or room; a raw tty read
with VMIN and VTIME both set can wait up to VTIME. Cost: a large blocking-pipe
write from a fiber is ~400 MB/s on macOS (PIPE_BUF 512) against ~3 GB/s for the
old O_NONBLOCK path; small pipe round trips got ~4.5x faster (raw fstat instead
of a pool-offloaded one). The old path also returned short writes from fds the
caller had left blocking; a write now returns its full length, as a blocking
write does. The socket-object patches still force O_NONBLOCK on
every socket they touch (`_make_nonblocking`, by design, gevent-style); a
socket shared with a child process or a `dup` sees it. The other
`set_blocking` calls in monkey/ and aio/ are on fds stackweave created.

### 14. Fibers that pin themselves onto a busy hub wait ~64 time slices

`p06_pin_starve.py`: a fiber doing `g.pin(0); sleep(0)` while hub 0 runs a
CPU-bound fiber first runs after 3.4 s; with
`STACKWEAVE_READY_STARVE_BOUND=4`, 0.18 s. The preempted hog returns to the
ready ring, which is picked before the global run-queue holding the pinned
fiber, until `starve_bound` (64) forces one global pull: 64 x 50 ms. `G.pin`
is the workaround the identity gaps recommend.

Status: FIXED on fix/ft-sched-fork: the pick right after a preempted ring
turn is the starve-bound forced turn (global run-queue / deque), so the pinned
fiber runs one slice after it is queued (p06: 0.03-0.06 s) and pinned fibers
take turns (`p05 mn_pin` / `run_pin`: ABAB...). A fiber a forced turn picked
hands the next pick back to the ring, so ring and waiting work alternate and a
stream of fresh CPU-bound fibers cannot starve the ring. The preempted fiber
stays on the ready ring, so it still never moves.
`stats()["mn_preempt_turns"]` counts the turns; the test checks it, as do
`tests/test_sched_fairness.py::test_preemption_takes_turns_between_fibers_pinned_onto_one_hub`
and `::test_preempted_fiber_runs_while_fresh_cpu_bound_fibers_keep_arriving`.

### 15. Each fiber gets its own bytecode copy (TLBC): cost

`p21_tlbc_per_fiber.py 2000`: 2000 live fibers that ran one 144 KB function
take 670 MB with TLBC on vs 281 MB with `PYTHON_TLBC=0` (~195 KB per fiber,
one copy each), and RSS does not come back after they exit. Every per-g
tstate reserves its own TLBC index (`Python/pystate.c:1652`); stackweave has
no TLBC handling. Sharing the hub's index would let a migrated frame keep
executing the old hub's copy while that hub specializes it -- the race TLBC
exists to avoid -- so the fix is not just "use the hub's index". A candidate
for the open post-burst RSS rise.

## Upstream CPython bugs (plain threads too)

### 16. Shared `BufferedReader.readline()` loses data

`p02t_buffered_threads.py`, no stackweave: FAIL on 3.13.12 (GIL), 3.14.6
(GIL), 3.14.4t. `_buffered_readline` scans the buffer before taking the lock
(`Modules/_io/bufferedio.c:1203`, "can run unlocked because ... can't trigger
any thread switch") and uses that stale `n`/`start` after `ENTER_BUFFERED`;
when another thread filled and partly consumed the buffer meanwhile,
`_bufferedreader_reset_buf` drops the rest. Two threads `readline()` on one
socket file, peer sends `b"one\ntwo\n"`: one gets `one\n`, the other blocks
forever.

### 17. A child forked from a non-main thread handles signals only at EINTR

`p19_fork_signal.py thread|mn`: an async SIGALRM handler never runs during a
1 s bytecode loop in the child (`plain`: 51 ms). `_PyRuntime.main_tstate` is
set once (`Python/pylifecycle.c:673`) and never updated after fork, and
`_PyEval_SignalReceived` sets the pending bit there; in such a child that is
the original main thread's (deleted) state. It is `interp->_initial_thread`,
embedded in the interpreter, so no write lands in freed memory. Under M:N
every fork comes from a hub thread.

Status: FIXED for forks made in a fiber on fix/ft-sched-fork: a
`pthread_atfork` child handler, which runs inside `fork()` before CPython's
after-fork code, points `_PyRuntime.main_tstate` at the forking fiber's per-g
state, the one that goes on running bytecode on the thread CPython then makes
main (`_PyRuntime.main_thread`) (`p19 mn`: 50 ms). It takes a ref on the
fiber's g that the child never drops, so stackweave never clears that state
there. Pointing at the embedded initial state when the state is cleared (the
first version) was wrong: the fork resets that state for reuse, so the next
thread state made in the child takes it. And a later fork from a plain thread
of that child deletes the fiber's state: the handler then puts back the
embedded initial state, CPython's own value, or `main_tstate` would name freed
memory. In the child, `run(1)` and the loop after it handle signals too;
`run(n>1)` there is refused as before (documented unsupported: `_mn_active`
is inherited). The same handler gives the fiber's state back its own
allocator: it borrowed the hub's (alloc-home), and the after-fork code
deletes the hub's state, so the child allocated through freed memory --
Linux 3.14 segfaulted in threading's after-fork hook (`llist_remove` in
`_PyMem_mi_page_clear_qsbr`), the cause of test_import_lock's Linux-3.14
known gap, now closed. A fork from a plain non-main thread is upstream's alone
(`p19 thread` still has no in-loop handler: that thread's state is freed when
the thread ends, with no hook to un-name it). The tests check
`stackweave_c._main_thread_state()` in the child, in a thread started there,
and in a grandchild forked from that thread.

## Known, deterministic

### 18. Main-thread-only APIs fail in every M:N fiber

`p08_mainthread.py [patch]`: `signal.signal` and `signal.set_wakeup_fd` raise
ValueError, `current_thread() is main_thread()` is False, in every fiber
including `main`, with or without monkey.patch (documented in
`monkey/signals.py`). `asyncio.run` skips its SIGINT handler;
`loop.add_signal_handler` and `unittest -c` fail.

## Candidates from the source audit, not reproduced

Plausible from reading the code; no probe yet.

- `_asyncio` task list (`Modules/_asynciomodule.c:2198-2240`): `unregister_task`
  takes an unlocked fast path when `task_tid == _Py_ThreadId()`; a task
  created on hub A by a fiber now on hub B, destroyed in A's merge drain,
  edits the fiber's list concurrently with B: list corruption.
- Dict lookup that migrates inside a key's `__eq__` (`Objects/dictobject.c`
  1315-1560): it started as the owner (no shared mark), finishes as a
  non-owner while the owner hub frees the old keys at once. Needs ASan/TSan.
- `pdb` / `breakpoint()` (monitoring backend) filters events by
  `current_thread()`: other fibers on the hub get stepped, a migrated fiber's
  own steps get dropped.
- `reprlib.recursive_repr` (dataclasses, ChainMap) keys on
  `(id(self), get_ident())`: a same-hub fiber reprs a preempted fiber's object
  as `'...'`.
- `current_thread()` builds a new `Dummy-N` per fiber (per-tstate
  `_thread_local_info`), so `%(threadName)s` is meaningless and each
  short-lived fiber that logs builds a Thread object.
- `multiprocessing.util.ForkAwareLocal` registers an after-fork entry per
  fiber that touches a manager proxy (leak), and each opens its own manager
  connection.
- `shelve` / `dbm.open` default to `dbm.sqlite3` with `check_same_thread=True`:
  the known sqlite gap through an implicit path.
- Per-tstate refcount arrays for the unique-id pool (`Python/uniqueid.c:54`):
  up to 512 KB per fiber that creates a heap-type instance or function, and
  merged under stop-the-world at every GC.
- The single-frame preemption backstop (`_PyEval_AddPendingCall`) sets the
  pending bit on every tstate under `HEAD_LOCK` and is consumed by whichever
  hub checks first.
- Single-phase extension init that parks: `pkgcontext` thread-local
  (`Python/import.c:821`).
- In-progress hook (`fix/rlock-fiber-identity`): `_Py_fiber_park.prepare`
  should return NULL unless the attached tstate is `g->tstate`, else a
  contended lock inside a subinterpreter call re-attaches the wrong
  interpreter's state on resume.
- Diagnostics: faulthandler dumps at most 100 tstates (newest first);
  `pthread_sigmask` changes the hub's mask for every fiber on it;
  `settrace_all_threads` misses fibers spawned later.

## Checked and fine

- `PyGILState_Ensure` from C inside a fiber (sqlite3 UDF, ctypes callback),
  2 hubs (`p01_gilstate.py`): `tstate_activate` rebinds the gilstate slot.
- Critical sections across a park or preemption.
- A hub blocked in C does not stall fd wakes for other hubs
  (`p03_blocked_hub_netpoll.py`).
- `queue.SimpleQueue` is replaced by monkey.patch (`monkey/queues.py`).
- Resurrection inside a migrating `__del__` (`Objects/object.c:495`) routes
  the reference through the merge queue; the second dealloc skips the
  finalizer.
- Locks never held across Python code: weakref striped locks, type lock,
  interned strings, codecs, audit hooks, `LOCK_POOL`, once-flags, zlib/bz2/
  lzma/zstd/hashlib object locks, `TextIOWrapper` (critical sections).
- Per-tstate (so per-fiber, as intended): contextvars, `_thread._local`,
  `Py_ReprEnter`, exc_info, recursion counters, asyncio's running loop,
  `_decimal` context.
