/* Part of mn_sched.c -- #included into that translation unit, not compiled
 * standalone.  See mn_sched.c for the includes/typedefs/file-scope state
 * these definitions depend on. */


/* Persistent worker-thread PyThreadState (see mn_sched.h).  Created on the
 * worker's OWN thread so biased-refcount + mimalloc bind to it, serialized
 * against the new_threadstate() immortalize race via runloom_hub_tstate_lock --
 * exactly like the hub threads.  NULL when the M:N interpreter isn't up. */
PyThreadState *runloom_mn_worker_tstate_new(void)
{
    PyThreadState *ts;
    if (runloom_mn_interp == NULL) return NULL;
    RUNLOOM_RLOCK(&runloom_hub_tstate_lock, RUNLOOM_RANK_HUB_TSTATE);
    ts = PyThreadState_New(runloom_mn_interp);
    RUNLOOM_RUNLOCK(&runloom_hub_tstate_lock, RUNLOOM_RANK_HUB_TSTATE);
    return ts;
}

void runloom_mn_worker_tstate_delete(PyThreadState *ts)
{
    if (ts == NULL) return;
    /* Delete on THIS (owning) thread: PyThreadState_New gilstate-bound ts to this
     * thread's gilstate-TSS slot, so it must be cleared/deleted here (mirrors the
     * hub thread self-delete in mn_sched_hub_main.c.inc).  RestoreThread first
     * waits out any pending stop-the-world, then attaches so DeleteCurrent clears
     * the right gilstate slot.  ts is DETACHED on entry (the worker SaveThread's
     * after every job). */
    PyEval_RestoreThread(ts);
    PyThreadState_Clear(ts);
    PyThreadState_DeleteCurrent();
}

int runloom_mn_init(int n_threads, int offload_hubs)
{
    int i;
    PyInterpreterState *interp;
    PyThreadState *main_ts;
    PyThreadState *saved;

#if !RUNLOOM_MN_CTRL
    /* The seeded controlled scheduler is disabled until it is re-implemented
     * for migration (see TODO(migration) in mn_sched_hub_resume_preempt.c.inc).
     * Refuse a seeded run outright: running it uncontrolled would hand back a
     * schedule that merely looks reproducible. */
    {
        const char *mnseed = getenv("STACKWEAVE_MN_SEED");
        const char *simmn = getenv("STACKWEAVE_SIM_MN");
        if ((mnseed != NULL && mnseed[0] != '\0') ||
            (simmn != NULL && simmn[0] != '\0' && simmn[0] != '0')) {
            PyErr_SetString(PyExc_RuntimeError,
                "mn_init: the seeded M:N scheduler (STACKWEAVE_MN_SEED / "
                "STACKWEAVE_SIM_MN) is disabled in migration mode: woken fibers "
                "run from the global run-queue, which the seeded baton does not "
                "order, so replay would not be deterministic (TODO: re-implement)");
            return -1;
        }
    }
#endif
    /* MN_SIM_DST_PLAN.md I0: RUNLOOM_SIM with the M:N scheduler is NOT yet
     * natively supported, and the failure mode is SILENT corruption, not an
     * error: the legacy single-thread sim pump on a hub thread lazily allocates
     * a WRONG per-thread sched (netpoll_sim.c.inc:46), so wait_fd timeouts
     * collapse instantly (a 5000ms timeout returns rc=0 at ~3ms) and the
     * fiber's BlockingIOError is swallowed while mn_run exits 0.  Fail LOUDLY
     * instead.  RUNLOOM_SIM_MN=1 opts into the native mn-sim bring-up path
     * (functional from increment I2; until then it only silences this fence
     * for bring-up work).  Checked before the already-inited early return:
     * the combo is unsupported no matter when it is asked for. */
    if (runloom_sim_enabled()) {
        const char *simmn = getenv("STACKWEAVE_SIM_MN");
        if (simmn == NULL || simmn[0] == '\0' || simmn[0] == '0') {
            PyErr_SetString(PyExc_RuntimeError,
                "mn_init: STACKWEAVE_SIM (set to any non-empty value; it is the "
                "sim seed) with the M:N scheduler is not yet supported (the "
                "legacy single-thread sim pump silently corrupts wait_fd "
                "timeouts on hub threads); set STACKWEAVE_SIM_MN=1 to opt into "
                "the native mn-sim bring-up path "
                "(docs/dev/soak/MN_SIM_DST_PLAN.md)");
            return -1;
        }
        /* The native path is the CENSUS dispatch, which only exists under the
         * controlled+BARRIER scheduler -- an opted-in mn-sim run without the
         * seed, or with RUNLOOM_MN_BARRIER=0 (the A/B knob), never arms the
         * census: every mn-sim gate keys on ctrl_armed and would go dark,
         * re-opening the probe-P4 silent corruption (review-reproduced: the
         * 5000ms timeout collapsed to 0.0001s again under BARRIER=0).  Fail
         * loudly on every such combination (I2). */
        {
            const char *mnseed = getenv("STACKWEAVE_MN_SEED");
            const char *bar = getenv("STACKWEAVE_MN_BARRIER");
            if (mnseed == NULL || mnseed[0] == '\0') {
                PyErr_SetString(PyExc_RuntimeError,
                    "mn_init: STACKWEAVE_SIM_MN=1 (native mn-sim) requires "
                    "STACKWEAVE_MN_SEED -- the sim ready ledger is dispatched by "
                    "the seeded census (docs/dev/soak/MN_SIM_DST_PLAN.md I2)");
                return -1;
            }
            if (bar != NULL && bar[0] == '0') {
                PyErr_SetString(PyExc_RuntimeError,
                    "mn_init: STACKWEAVE_SIM_MN=1 (native mn-sim) is incompatible "
                    "with STACKWEAVE_MN_BARRIER=0 -- without the barrier the "
                    "census never arms and the sim gates go dark "
                    "(docs/dev/soak/MN_SIM_DST_PLAN.md I2)");
                return -1;
            }
            /* A wall-clock preempt time-slicer started BEFORE the sim env was
             * set would keep posting nondeterministic yields into the seeded
             * run (the preempt_init gate only refuses NEW slicers). */
            if (runloom_preempt_active()) {
                PyErr_SetString(PyExc_RuntimeError,
                    "mn_init: a preempt_init time-slicer is running -- call "
                    "preempt_fini() before a native mn-sim run (wall-clock "
                    "preemption is nondeterministic)");
                return -1;
            }
        }
    }

    if (runloom_hubs != NULL) return 0;  /* already inited */
    runloom_introspect_init();   /* registry lock; idempotent (also done in PyInit) */
    if (n_threads <= 0) {
        /* Auto: min(cores, 16).  Agent 6 measured runloom on 3.13t scaling
         * linearly to H=16 (~1.17 M ops/sec) then REGRESSING past H=32
         * for Python workloads -- atomic-refcount cache-line contention,
         * the same ceiling threads/asyncio hit.  16 is the right default
         * for Python; an explicit runloom_mn_init(H>16) is still honored
         * (worthwhile only for pure-C fiber workloads). */
        n_threads = runloom_cpu_count();
        if (n_threads <= 0) n_threads = 4;
        if (n_threads > 16) n_threads = 16;
    }
    /* Reserve the offload hubs ON TOP of the general count, so enabling the
     * feature never shrinks general parallelism -- n_threads keeps whatever
     * the caller (or the auto-sizing above) chose, and K extra hubs are
     * appended.  They occupy the TAIL of the array, which is what lets every
     * exclusion below be a bound rather than a per-hub test.  Resolved here,
     * once, before any hub thread exists.  The count comes only from the
     * caller: it is a property of what the CALLER's code does (does it make
     * blocking calls?), not of how the process was launched.  Negative = 0. */
    runloom_offload_hub_count = offload_hubs;
    if (runloom_offload_hub_count > RUNLOOM_OFFLOAD_HUBS_MAX)
        runloom_offload_hub_count = RUNLOOM_OFFLOAD_HUBS_MAX;
    if (runloom_offload_hub_count < 0)
        runloom_offload_hub_count = 0;
    if (runloom_offload_hub_count > 0 &&
        n_threads > INT_MAX - runloom_offload_hub_count) {
        runloom_offload_hub_count = 0;          /* absurd n_threads; stay off */
    }
    /* No cap check against RUNLOOM_PARKER_POOL_HUBS here.  An earlier version
     * refused n+K past it, which broke a SUPPORTED configuration: hubs beyond
     * the per-hub pools share the default pool -- correct, just without the
     * locality -- and tests/test_cov100_wake_iouring.py deliberately runs 100
     * hubs to exercise exactly that fallback.  It was also the wrong worry for
     * offload hubs specifically: they never park on an fd, so which parker pool
     * they map to does not matter to them at all. */
    n_threads += runloom_offload_hub_count;

    runloom_hubs = (runloom_hub_t *)PyMem_Calloc((size_t)n_threads, sizeof(runloom_hub_t));
    if (runloom_hubs == NULL) {
        runloom_offload_hub_count = 0;
        PyErr_NoMemory();
        return -1;
    }
    runloom_hub_count = n_threads;
    main_ts = PyThreadState_Get();
    interp = main_ts->interp;
    /* Migration mode: capture the interp for the per-g PyThreadState_New and
     * stand the snap down BEFORE any hub thread starts running fibers.  Always
     * on for an M:N run; mn_fini clears it again. */
    runloom_mn_interp = interp;
    runloom_set_per_g_tstate_mode(1);
    /* Configure the sysmon watchdog (threshold, logging) BEFORE the hubs
     * spawn so the per-resume progress instrumentation is live immediately. */
    runloom_sysmon_config();
    /* controlled M:N (RUNLOOM_MN_SEED): set up the execution baton before any hub
     * runs.  No-op when RUNLOOM_MN_SEED is unset, and always while the
     * controller is disabled (RUNLOOM_MN_CTRL). */
    runloom_mn_ctrl_init(n_threads);
    /* Its frame-count preemption hook, installed while we still hold the main
     * tstate and before any hub runs Python (no-op unless controlled). */
    runloom_preempt_install(interp);
    /* Native mn-sim EFFECTIVE-state check (I2 review): ctrl_init silently
     * self-disables on OOM -- the env fence above cannot see that, and an
     * un-controlled sim+mn run re-opens the probe-P4 corruption.  Verified
     * BEFORE any hub thread spawns, so the unwind is just the allocs above. */
    if (runloom_sim_enabled() && !runloom_mn_ctrl_controlled()) {
        const char *simmn = getenv("STACKWEAVE_SIM_MN");
        if (simmn != NULL && simmn[0] != '\0' && simmn[0] != '0') {
            PyMem_Free(runloom_hubs);
            runloom_hubs = NULL;
            runloom_hub_count = 0;
            runloom_offload_hub_count = 0;   /* keep the boundary consistent across mn_init cycles */
            PyErr_SetString(PyExc_RuntimeError,
                "mn_init: the controlled scheduler failed to initialise "
                "(OOM?) -- native mn-sim cannot run without the census "
                "(docs/dev/soak/MN_SIM_DST_PLAN.md I2)");
            return -1;
        }
    }

    /* Init-once the lock that serializes the hubs' PyThreadState_New (see its
     * declaration in mn_sched.c).  mn_init is single-threaded here, before any
     * hub spawns, so the flag needs no atomics. */
    if (!runloom_hub_tstate_lock_inited) {
        runloom_mutex_init(&runloom_hub_tstate_lock);
        runloom_hub_tstate_lock_inited = 1;
    }

    for (i = 0; i < n_threads; i++) {
        runloom_hub_t *h = &runloom_hubs[i];
        h->id = i;
        runloom_sched_init(&h->sched);
        runloom_cldeque_init(&h->deque);
        runloom_mutex_init(&h->sub_lock);
        h->sub_tail = NULL;
        __atomic_store_n(&h->sub_head, (runloom_g_t *)NULL, __ATOMIC_RELAXED);
        /* BUG #10 per-hub idle wake (see mn_sched.c sub_gen). */
        h->sub_gen = 0;
        runloom_mutex_init(&h->idle_lock);
        runloom_cond_init(&h->idle_cond);
        h->idle_waiting = 0;
        /* Controlled-replay deferred-publish staging (barrier mode only): one
         * per-target list so a segment's cross-hub submits flush atomically at
         * release.  PyMem_Calloc zeroes; OOM -> NULL -> hub_submit falls back to
         * the immediate (non-deterministic) push, still correct. */
        h->stage_head = NULL;
        h->stage_tail = NULL;
        h->stage_pending = 0;
        if (runloom_mn_ctrl.enabled && runloom_mn_ctrl.barrier) {
            h->stage_head = (runloom_g_t **)PyMem_Calloc((size_t)n_threads,
                                                         sizeof(runloom_g_t *));
            h->stage_tail = (runloom_g_t **)PyMem_Calloc((size_t)n_threads,
                                                         sizeof(runloom_g_t *));
            if (h->stage_head == NULL || h->stage_tail == NULL) {
                PyMem_Free(h->stage_head); h->stage_head = NULL;
                PyMem_Free(h->stage_tail); h->stage_tail = NULL;
            }
        }
        /* h->tstate is created BY THE HUB THREAD (in runloom_hub_main), NOT
         * here.  PyThreadState_New eagerly binds the tstate to the CALLING
         * thread: bind_tstate() -> _Py_brc_init_thread() + the mimalloc bind,
         * which CPython documents "needs to be initialized from the active
         * thread."  Binding on the main thread stamps the hub tstate's
         * biased-refcount owner id (brc->tid) with MAIN's thread id, while
         * every object the hub later creates gets ob_tid = the HUB thread's id.
         * A cross-thread DECREF of such an object then can't find its owner in
         * the brc hash table (find_thread_state() looks up by ob_tid and
         * misses), so CPython concludes the owner "must have exited" and merges
         * the refcount out from under the still-running hub -- a data race on
         * ob_ref_local -> torn refcount -> premature free -> use-after-free (the
         * gc-churn SIGSEGV the hang-hunter found).  Creating the tstate on the
         * hub's own thread makes brc->tid == ob_tid for every object the hub
         * owns, so the owner is always found.  PyMem_Calloc already zeroed
         * h->tstate; sysmon / hub-snapshot / world-yield read it with an acquire
         * load and treat NULL as attach-state -1 (fail closed) during the brief
         * window before the hub publishes it. */
    }
    saved = PyEval_SaveThread();
    for (i = 0; i < n_threads; i++) {
        if (runloom_thread_create(&runloom_hubs[i].thread,
                               runloom_hub_main, &runloom_hubs[i]) != 0) {
            /* Mark the unspawned hubs as already-stopping + join the
             * ones we did spawn before returning -1. */
            int j;
            for (j = i; j < n_threads; j++) {
                __atomic_store_n(&runloom_hubs[j].stopping, 1, __ATOMIC_RELEASE);
            }
            for (j = 0; j < i; j++) {
                __atomic_store_n(&runloom_hubs[j].stopping, 1, __ATOMIC_RELEASE);
                runloom_thread_join(runloom_hubs[j].thread);
            }
            PyEval_RestoreThread(saved);
            for (j = 0; j < n_threads; j++) {
                /* tstate is created by the hub thread, so an unspawned hub (or
                 * one that failed its own PyThreadState_New) leaves it NULL.
                 * Spawned hubs were joined just above, so any tstate is now
                 * detached and safe to clear/delete from this (main) thread. */
                PyThreadState *hts = runloom_hubs[j].tstate;
                if (hts != NULL) {
                    PyThreadState_Clear(hts);
                    PyThreadState_Delete(hts);
                }
                runloom_mutex_destroy(&runloom_hubs[j].sub_lock);
                runloom_mutex_destroy(&runloom_hubs[j].idle_lock);
                runloom_cond_destroy(&runloom_hubs[j].idle_cond);
            }
            PyMem_Free(runloom_hubs);
            runloom_hubs = NULL;
            runloom_hub_count = 0;
            runloom_offload_hub_count = 0;   /* keep the boundary consistent across mn_init cycles */
            PyErr_SetString(PyExc_OSError, "thread spawn failed");
            return -1;
        }
    }
    PyEval_RestoreThread(saved);
    /* Resolve the depot auto-cap's SAFE_MAX (reads /proc once) before the watchdog,
     * whose tick will recompute the cap from the live-stack watermark. */
    runloom_stack_autocap_init();
    /* Hubs are up; start the sysmon watchdog. */
    runloom_sysmon_spawn();
    return n_threads;
}

/* Release any gs still parked in this hub's queues after the hub
 * thread has exited.  Hub thread is already joined by the caller, so
 * sub_head / deque / sched.ready / sched.sleep_heap are quiescent and
 * safe to drain from a single thread regardless of which primitive
 * they normally use for concurrent access.
 *
 * Each leaked g still holds: its Python callable ref, an fcontext
 * stack, and a slab entry.  runloom_g_decref unwinds all three when its
 * last reference drops.  The hub holds exactly one ref per g it
 * accepted (taken at spawn in runloom_mn_fiber_core, dropped at completion
 * in hub_main), so a single decref here mirrors the completion path. */
static void runloom_mn_hub_drain_leftovers(runloom_hub_t *h)
{
    runloom_g_t *sub;
    runloom_g_t *g;
    sub = h->sub_head;
    h->sub_tail = NULL;
    __atomic_store_n(&h->sub_head, (runloom_g_t *)NULL, __ATOMIC_RELAXED);  /* census x-thread read */
    while (sub != NULL) {
        runloom_g_t *next = sub->next;
        sub->next = NULL;
        runloom_g_decref(sub);
        sub = next;
    }
    /* Residual deferred-publish staging (barrier mode): a segment always flushes
     * at release, so these are normally empty -- drain defensively for an
     * abort/early-exit path so a staged g's hub ref is not leaked. */
    if (h->stage_head != NULL) {
        int t;
        for (t = 0; t < runloom_hub_count; t++) {
            runloom_g_t *s = h->stage_head[t];
            h->stage_head[t] = h->stage_tail[t] = NULL;
            while (s != NULL) {
                runloom_g_t *next = s->next;
                s->next = NULL;
                runloom_g_decref(s);
                s = next;
            }
        }
        h->stage_pending = 0;
    }
    while ((g = runloom_sched_ready_pop(&h->sched)) != NULL)
        runloom_g_decref(g);
    while ((g = (runloom_g_t *)runloom_cldeque_pop(&h->deque)) != NULL)
        runloom_g_decref(g);
    while (h->sched.sleep_size > 0) {
        g = runloom_sched_sleep_pop(&h->sched);
        if (g != NULL) runloom_g_decref(g);
    }
    /* A signal committed to a fiber on this hub that never resumed to take it
     * (the run was torn down first, or the g completed).  Drop it rather than
     * leak the exception AND -- the part that actually bites -- rather than
     * leave the delivery slot occupied: runloom_netpoll_signal_wake reserves
     * that slot with a NULL->exc CAS, so a stale one makes every future signal
     * to this hub skip it silently.  Hubs are reused across runs. */
    {   PyObject *stale = __atomic_exchange_n(&h->sched.signal_exc, NULL,
                                              __ATOMIC_ACQ_REL);
        Py_XDECREF(stale);
        h->sched.signal_target = NULL;
    }
}

void runloom_mn_fini(void)
{
    int i;
    if (runloom_hubs == NULL) return;
    /* Bump the session generation FIRST (RELEASE), before any hub state is torn
     * down, so the ENTIRE teardown window -- not just the post-free tail -- is
     * covered: a RunloomG handle that outlives this run (a foreign thread still
     * holding one and calling .wake()) sees the changed generation and treats
     * its g's park_hub as a no-op, never dereferencing a hub that is mid-
     * teardown or already freed.  All hubs' gs have completed by now (mn_run
     * joins on the pending count), so there is no legitimate in-session wake to
     * lose by gating early.  See runloom_mn_gen in mn_sched.c and the dekker
     * finding (docs/dev/repro/DEKKER_SIGSEGV_FINDING.md). */
    __atomic_add_fetch(&runloom_mn_gen, 1, __ATOMIC_RELEASE);
    /* Stop the watchdog before any hub teardown so its scan never touches
     * freed hub state. */
    runloom_sysmon_stop_join();
    for (i = 0; i < runloom_hub_count; i++) {
        __atomic_store_n(&runloom_hubs[i].stopping, 1, __ATOMIC_RELEASE);
        /* BUG #10 idle wake: a hub parked in its idle condvar must observe the
         * stop promptly instead of waiting out idle_ns.  Signal under the lock
         * so it can't slip between the hub's stopping re-check and its wait.
         * With the idle backoff the wait can be a deep one in ANY mode, so kick
         * every mode (mirrors runloom_mn_kick_all_hubs) -- else a hub deep in its
         * epoll pump would stall the join below up to the full cap (seconds at a
         * large RUNLOOM_IDLE_BACKOFF_MS). */
        runloom_mutex_lock(&runloom_hubs[i].idle_lock);
        runloom_cond_signal(&runloom_hubs[i].idle_cond);
        runloom_mutex_unlock(&runloom_hubs[i].idle_lock);
        runloom_netpoll_wake_pump((void *)&runloom_hubs[i]);
    }
    {
        PyThreadState *saved = PyEval_SaveThread();
        for (i = 0; i < runloom_hub_count; i++) {
            runloom_thread_join(runloom_hubs[i].thread);
        }
        PyEval_RestoreThread(saved);
    }
    /* controlled-mode baton: all hubs joined, nobody can acquire/release now */
    runloom_mn_ctrl_fini();
    /* Drain leftover gs BEFORE tearing down per-hub tstates: g->snap
     * may reference per-tstate state (exc_info, datastack chunks) that
     * pystate_snap_clear needs to release while the tstate is still
     * walkable.  runloom_g_decref also runs Py_XDECREF on callable /
     * result / error, which needs the main thread's GIL -- already
     * held here (we restored above). */
    for (i = 0; i < runloom_hub_count; i++) {
        runloom_mn_hub_drain_leftovers(&runloom_hubs[i]);
    }
    /* Drain the global run-queue too.  Exactly-once-wake means an entry here
     * is a single, live, yet-to-run woken g (no stale duplicates) -- but it
     * holds TWO refs: its scheduler ref and the entry's queue ref that wake_g
     * took on PARKED->QUEUED (the resume path drops both at completion).
     * Dropping only one leaked the g and its PyThreadState past run().  Hubs
     * are joined, so this is single-threaded -- no lock needed. */
    {
        runloom_g_t *g = runloom_global_runq_head;
        runloom_global_runq_head = runloom_global_runq_tail = NULL;
        __atomic_store_n(&runloom_global_runq_len, 0, __ATOMIC_RELEASE);
        /* Must be zeroed with its sibling: this is a file-scope static (the
         * per-hub runq_pinned_n is Calloc'd fresh per session), so a leftover
         * entry would carry a phantom count into the NEXT session. */
        __atomic_store_n(&runloom_global_runq_unpinned_len, 0, __ATOMIC_RELEASE);
        while (g != NULL) {
            runloom_g_t *next = g->next;
            g->next = NULL;
            if (__atomic_load_n(&g->wake_state, __ATOMIC_ACQUIRE) == RUNLOOM_WS_QUEUED)
                runloom_g_decref(g);   /* queue ref */
            runloom_g_decref(g);       /* scheduler ref */
            g = next;
        }
    }
    for (i = 0; i < runloom_hub_count; i++) {
        /* A hub that ran deletes its OWN tstate on its own thread at hub_main
         * exit (it was gilstate-bound to that thread; a cross-thread delete from
         * here corrupts the main thread's gilstate binding -- see the comment in
         * mn_sched_hub_main.c.inc), publishing tstate = NULL, so this is normally
         * NULL.  Non-NULL only for a hub that allocated a tstate but never
         * reached its self-delete (a crash/abort path); clear it here as a
         * best-effort fallback.  Hubs are joined by now, so this is unraced. */
        PyThreadState *hts = runloom_hubs[i].tstate;
        if (hts != NULL) {
            /* trace conformance: deleter is the MAIN thread (-1).  Normally
             * unreached (hubs self-delete); fires under the negative-control
             * RUNLOOM_GILSTATE_DELETE_ON_MAIN, the pre-c28e5ca bug path. */
            runloom_gilstate_trace("Delete", i, -1);
            PyThreadState_Clear(hts);
            PyThreadState_Delete(hts);
        }
        runloom_mutex_destroy(&runloom_hubs[i].sub_lock);
        runloom_mutex_destroy(&runloom_hubs[i].idle_lock);
        runloom_cond_destroy(&runloom_hubs[i].idle_cond);
        /* Release the g-refs held by this hub's still-pending timer entries
         * (future-deadline timed parks that never fired) so they don't leak,
         * then free the timer-heap buffer (otherwise leaked -- it was never
         * freed). */
        runloom_sched_release_timers(&runloom_hubs[i].sched);
        PyMem_Free(runloom_hubs[i].sched.timer_heap);
        runloom_hubs[i].sched.timer_heap = NULL;
        runloom_hubs[i].sched.timer_cap = 0;
        /* Free the per-hub ready-ring buffer that runloom_sched_init allocated
         * (PyMem_Calloc(64) = 512 B/hub).  It was otherwise leaked every
         * mn_init/mn_fini cycle -- the ONLY PyMem_Free(ready_ring) was the
         * old-ring free inside runloom_ready_grow, never a teardown.  The ring
         * holds raw slab-owned g pointers (no held refs, unlike timer_heap), so
         * freeing the buffer at quiescent teardown releases nothing else. */
        PyMem_Free(runloom_hubs[i].sched.ready_ring);
        runloom_hubs[i].sched.ready_ring = NULL;
        runloom_hubs[i].sched.ready_cap = 0;
        runloom_hubs[i].sched.ready_mask = 0;
        PyMem_Free(runloom_hubs[i].stage_head);
        runloom_hubs[i].stage_head = NULL;
        PyMem_Free(runloom_hubs[i].stage_tail);
        runloom_hubs[i].stage_tail = NULL;
    }
    /* Reclaim the g-struct slab pool: fold in the main slab + return the whole
     * pool to the OS, closing the per-fiber RSS leak across mn_init/mn_fini.
     * Every HUB thread is joined here -- but the blockpool WORKER threads are NOT
     * (runloom_blockpool_fini is never called; they persist across cycles).  A
     * worker finishing a job whose fiber was resumed EARLY (a spurious/cancel
     * wake, runloom_blockpool.c:200) still calls runloom_sched_wake_safe(g) -> an
     * atomic g->wake_pending write AFTER that fiber completed and its g was
     * retained onto the slab -- and mn_run returns on per-hub-pending==0 without
     * ever consulting bp_inflight.  So free()ing the slab now can race that
     * write: a real (rare, teardown) use-after-free.  Wait for the blockpool to
     * quiesce first; inflight()==0 (ACQUIRE) pairs with the worker's
     * post-wake_safe bp_inflight-- (ACQ_REL), so every wake_safe is then complete
     * and ordered before the free.  Bounded (~1s) so a wedged worker can't hang
     * teardown -- reclaim() must run before the registry/interp teardown below. */
    {
        int drain_spins = 0;
        while (runloom_blockpool_inflight() != 0 && drain_spins < 100000) {
            struct timespec nap = {0, 10000};   /* 10us */
            nanosleep(&nap, NULL);
            drain_spins++;
        }
    }
    /* runloom_g_slab_reclaim unlinks each g from the fiber registry BEFORE
     * free()ing it (runloom_sched_core.c.inc), and the leftover-drain above
     * snap_clear'd (valid=0) every abandoned parked g.  So after this returns the
     * registry holds no pointer into freed g structs, and no valid=1 snap points
     * into a chunk we are about to reclaim -- the GC frames anchor is safe across
     * teardown (any post-run gc.collect walks an empty/clean registry). */
    runloom_g_slab_reclaim();
    /* Free the datastack-chunk graveyard the hubs spliced their TLS chunk caches
     * into at exit.  Every hub is joined, so no foreign sys._current_frames reader
     * can still be inside a chunk's grace window -- returning them to the OS is
     * sound.  Closes the dominant M:N per-fiber datastack-chunk RSS leak.  Safe wrt
     * the GC frames anchor: every g was unlinked from the registry by the slab
     * reclaim just above, so no anchor walk can reach a frame in these chunks. */
    runloom_chunk_pool_reclaim();
    PyMem_Free(runloom_hubs);
    runloom_hubs = NULL;
    runloom_hub_count = 0;
    runloom_offload_hub_count = 0;   /* keep the boundary consistent across mn_init cycles */
    /* (No global pending counter to reset -- runloom_mn_run sums the per-hub
     * fields, which are freed along with the hub array just above.) */
    /* Stand the snap back up for any later single-thread scheduler use. */
    runloom_set_per_g_tstate_mode(0);
    runloom_mn_interp = NULL;
    /* Sysmon is joined -> no more auto-cap ticks; forget the watermark so the next
     * session (or a sysmon-less context) falls back to the static default. */
    runloom_stack_autocap_reset();
    /* (Session generation was bumped at the TOP of fini -- see the note there.) */
}

/* Reset the M:N scheduler in a forked CHILD.  After fork() only the
 * forking thread survives -- the hub OS threads are gone -- so we must
 * NOT join them (runloom_mn_run would hang forever waiting on dead hubs:
 * the reproduced fork deadlock) and must NOT free/clear their structs
 * (they hold PyThreadStates + locks tied to the dead threads).  Instead
 * abandon the inherited hub array (a one-time leak in a short-lived
 * child) and reset every global so the child sees an UNINITIALISED M:N
 * scheduler: a fresh runloom_mn_init() works, and until then runloom runs on
 * the single-thread scheduler.  The child is single-threaded at this
 * point, so no lock is taken -- we only re-init the global run-queue lock
 * in case a dead hub held it at fork time. */
void runloom_mn_reset_after_fork(void)
{
    runloom_hubs = NULL;            /* abandon (leak) the inherited hub array */
    runloom_hub_count = 0;
    runloom_offload_hub_count = 0;   /* keep the boundary consistent across mn_init cycles */
    runloom_mn_spawn_counter = 0;
    runloom_mn_nidle = 0;           /* WAKEP parked-hub count: no hubs in the child */
    runloom_mn_interp = NULL;
    runloom_set_per_g_tstate_mode(0);
    runloom_mutex_init(&runloom_global_runq_lock);   /* may be inherited held */
    runloom_global_runq_head = runloom_global_runq_tail = NULL;
    __atomic_store_n(&runloom_global_runq_len, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&runloom_global_runq_unpinned_len, 0, __ATOMIC_RELAXED);
    runloom_tls_hub = NULL;        /* the surviving thread is no longer a hub */
    runloom_tls_current_g = NULL;
    runloom_coro_prewarm_reset_after_fork();   /* daemon thread didn't survive */
    runloom_stack_autocap_reset();             /* sysmon gone -> forget the watermark */
    /* The inherited hub array is abandoned (leaked); bump the generation so a
     * handle carried across the fork can't route a wake into it.  See the bump
     * in runloom_mn_fini for the rationale. */
    __atomic_add_fetch(&runloom_mn_gen, 1, __ATOMIC_RELEASE);
}

/* Internal core: pick hub, alloc g, set up coro, submit, bump counters.
 * Either callable (Python path) or c_fn+c_arg (C-only path) must be set,
 * not both.  Returns 0 on success, -1 on failure (errno set).  Python
 * error is also set on failure when callable != NULL. */
/* Auto-pace: a tight in-fiber spawn loop pins its producer hub (a compiled
 * `for: fiber(fn)` loop has no yield points), so without this only 7/8 hubs run
 * fibers and the loser hub floods its run queue + cold stack pool.  Yielding
 * every PACE spawns lets the producer's hub interleave running with spawning --
 * the runtime self-pacing a busy spawner, exactly like Go preempting a goroutine
 * that spawns in a loop.  Default 512 (measured sweet spot: ~2.2M/s naked
 * single-spawn vs ~0.9M unpaced; light spawners never reach PACE so they pay
 * nothing).  A periodic cooperative yield, NOT a cap on in-flight fibers, so
 * high-concurrency PARKED workloads never starve.  Tune/disable via
 * RUNLOOM_SPAWN_PACE (0 = off). */
static long runloom_spawn_pace(void)
{
    static long v = -1;
    long cur = __atomic_load_n(&v, __ATOMIC_RELAXED);
    if (cur < 0) {
        const char *e = getenv("STACKWEAVE_SPAWN_PACE");
        cur = (e != NULL) ? atol(e) : 512;
        if (cur < 0) cur = 0;
        __atomic_store_n(&v, cur, __ATOMIC_RELAXED);
    }
    return cur;
}

/* force_hub: -1 = normal placement (general hubs only).  >=0 pins the spawn to
 * that hub index, which is how offload work reaches a reserved offload hub --
 * see the offload-hubs block in mn_sched.c.  Routed through THIS
 * function rather than a second spawn path on purpose: a path that bypassed
 * runloom_greg_link would reopen the parked-frame GC blind spot (CLAUDE.md,
 * "Parked-fiber frames are made GC-visible by the frames anchor"). */
static int runloom_mn_fiber_core(PyObject *callable, runloom_c_entry_fn c_fn,
                           void *c_arg, size_t stack_size, long py_index,
                           int force_hub)
{
    long n;
    int hub_idx;
    runloom_g_t *g;
    runloom_hub_t *h;
    if (runloom_hubs == NULL) {
        if (callable != NULL) {
            PyErr_SetString(PyExc_RuntimeError,
                            "runloom_mn_init() must be called first");
        }
        errno = EINVAL;
        return -1;
    }
    {   /* auto-pace (experiment): yield every PACE spawns so the producer hub
         * interleaves running with spawning.  Only an in-fiber spawner can yield. */
        long pace = runloom_spawn_pace();
        if (pace > 0 && runloom_mn_tls_current_g() != NULL) {
            static RUNLOOM_TLS long runloom_pace_ctr = 0;
            if (++runloom_pace_ctr >= pace) {
                runloom_pace_ctr = 0;
                runloom_mn_yield_current();
            }
        }
    }
    /* Bumped BEFORE the pin branch: the controlled-replay barrier keys on the
     * spawn_counter%H map, so a pinned spawn must still consume its index. */
    n = __atomic_fetch_add(&runloom_mn_spawn_counter, 1, __ATOMIC_RELAXED);
    if (force_hub >= 0 && force_hub < runloom_hub_count) {
        /* Pinned: overrides every placement policy, general-hub modulus
         * included, so this is the only way a g lands on an offload hub.
         * Both API boundaries validate; the range check is belt and braces. */
        hub_idx = force_hub;
    } else {
        /* EXCLUSION 1 of 3: general spawns are placed over general hubs only.
         * runloom_general_hub_count() == runloom_hub_count when the feature is
         * off, so all three branches below are bit-for-bit their previous
         * behaviour in the default build. */
        int gen = runloom_general_hub_count();
        if (gen > 1 && runloom_sched_random_on()) {
            /* Sanitizer/debug: randomize placement too (Go's raceenabled), so a
             * goroutine can land on any hub rather than the round-robin one --
             * diversifies cross-hub interleavings the sanitizer lane explores. */
            hub_idx = (int)(runloom_steal_rand(-1) % (unsigned)gen);
        } else {
            hub_idx = (int)(n % gen);
        }
    }
    h = &runloom_hubs[hub_idx];
    {   /* Backpressure: admit under the max-fibers limit. */
        int adm = runloom_fiber_admit();
        if (adm == 0) {
            if (callable != NULL)
                PyErr_Format(PyExc_RuntimeError,
                             "stackweave: fiber limit (%ld) exceeded",
                             runloom_get_max_fibers());
            errno = EAGAIN;
            return -1;
        }
        g = runloom_g_slab_alloc();
        if (g == NULL) {
            if (adm == 2) runloom_fiber_release();
            if (callable != NULL) PyErr_NoMemory();
            errno = ENOMEM;
            return -1;
        }
        g->limit_counted = (unsigned char)(adm == 2);
    }
    if (callable != NULL) {
        Py_INCREF(callable);
        g->callable = callable;
        /* Stack-advice: note the kind at spawn (0 unless profiling on). */
        g->advice_key = runloom_advice_note_spawn(callable);
        if (py_index >= 0) {                 /* fiber_n(indexed=True): call fn(index) */
            g->pass_index = 1;
            g->c_arg = (void *)(intptr_t)py_index;
        }
    } else {
        g->c_entry = c_fn;
        g->c_arg   = c_arg;
        g->advice_key = 0;   /* C-entry fibers aren't profiled */
    }
    g->refcount = 1;
    g->owner = &h->sched;   /* owning hub's sched -- groups fibers in the dump */
    __atomic_store_n(&g->id, runloom_next_goid(), __ATOMIC_RELAXED);
    g->park_fd = -1;
    /* Record the pin (+1 encoded, see runloom_sched.h) so the drain routes this
     * g to hub_idx's LOCAL FIFO, not its stealable deque.  Offload spawns are
     * pinned too: under a migration mode a woken offload fiber goes to the
     * GLOBAL RUNQ, and the pin is what stops a general hub pulling a blocking
     * call onto itself (exclusion 2 from the other side). */
    if (force_hub >= 0)
        __atomic_store_n(&g->pin_hub1, hub_idx + 1, __ATOMIC_RELEASE);
    /* Clamp an explicit per-spawn size to [MIN, MAX], exactly like the
     * single-thread go() path (runloom_sched_spawn_sized): "the explicit size
     * wins" means it overrides the autosizer, not that it can exceed the 8 MiB
     * ceiling -- an unclamped wild size fails runloom_coro_new's mmap with
     * MemoryError instead of clamping the way single-thread go() does. */
    if (stack_size > 0) {
        if (stack_size < RUNLOOM_MIN_STACK_SIZE) stack_size = RUNLOOM_MIN_STACK_SIZE;
        if (stack_size > RUNLOOM_MAX_STACK_SIZE) stack_size = RUNLOOM_MAX_STACK_SIZE;
    }
    /* Stack size, in precedence order:
     *  1. an explicit per-spawn override (mn_fiber(fn, stack_size) > 0) -- a caller
     *     that knows it runs a deep, NON-yielding C burst (cold stdlib import,
     *     readline/terminfo init, a doctest, OpenSSL) asks for a roomy stack up
     *     front; the resume-boundary copy-grow only rescues stacks that deepen
     *     ACROSS yields, not a single-frame burst;
     *  2. otherwise the adaptive autosizer -- this fiber-kind's learned
     *     size (start-large default for an unseen kind), which itself falls
     *     back to the hub default when autosize is off. */
    /* Resolve the stack size, then create the coro + guarded stack eagerly: the
     * resume path claims a g with its coro already set (a g with no coro or no
     * tstate is skipped as dead), and an oversize ask fails here, at spawn,
     * where the caller can handle the MemoryError. */
    g->req_stack_size = (stack_size > 0)
                          ? stack_size
                          : runloom_advice_size_for(g->advice_key, callable,
                                                    (size_t)h->sched.stack_size);
    /* Floor the resolved size for the FT-3.14 SP-guard headroom (p226).  Covers
     * both an explicit pin and an advice-learned auto size; no-op elsewhere. */
    g->req_stack_size = runloom_fiber_stack_floor(g->req_stack_size);
    g->coro = runloom_coro_new(g->req_stack_size, runloom_g_entry, g);
    if (g->coro == NULL) {
        if (callable != NULL) {
            Py_DECREF(callable);
            g->callable = NULL;             /* don't leave a dangling ref on the slab */
            PyErr_NoMemory();
        }
        if (g->limit_counted) {             /* release the admission slot we took */
            runloom_fiber_release();
            g->limit_counted = 0;
        }
        runloom_g_slab_free(g);             /* return to slab (handles greg + free) */
        errno = ENOMEM;
        return -1;
    }
    /* Wake-state machine init: a fresh g has never parked, so it is
     * conceptually owned/running from the moment a hub first resumes it
     * (slab-zeroed wake_state would be PARKED, which the QUEUED->RUNNING claim
     * and the park CAS both mis-handle).  Lift it to RUNNING up front. */
    __atomic_store_n(&g->wake_state, RUNLOOM_WS_RUNNING, __ATOMIC_RELEASE);
    /* Give the g its own migratable PyThreadState (runloom_mn_interp is set by
     * mn_init for the whole life of the hubs). */
    if (runloom_mn_interp != NULL) {
        g->tstate = PyThreadState_New(runloom_mn_interp);
        if (g->tstate == NULL) {
            runloom_coro_destroy(g->coro);
            g->coro = NULL;
            if (callable != NULL) {
                Py_DECREF(callable);
                g->callable = NULL;
                PyErr_NoMemory();
            }
            if (g->limit_counted) {         /* release the admission slot */
                runloom_fiber_release();
                g->limit_counted = 0;
            }
            /* slab_free, NOT PyMem_Free: the g came from runloom_g_slab_alloc
             * whose cold path is libc calloc -- PyMem_Free here was an allocator
             * mismatch (heap corruption risk), and it also leaked the limit. */
            runloom_g_slab_free(g);
            errno = ENOMEM;
            return -1;
        }
    }
    /* Count this spawn on the target hub's pending BEFORE publishing the g to
     * that hub.  runloom_mn_hub_submit kicks the hub (locked sub-list push +
     * sub_gen bump + idle_cond signal + netpoll/ring wake), so once submitted a
     * DIFFERENT hub (round-robin routing) can drain, run, and complete the g --
     * decrementing its pending via runloom_mn_pending_complete -- before the
     * matching increment lands.  Published-first, the g's net contribution would
     * transiently read -1 and cancel the spawner's own +1, letting
     * runloom_mn_run see a false sum of 0 while this spawner is still live.
     * Incrementing first keeps the sum over-counted, never under-counted --
     * the same rule runloom_mn_pending_steal uses.  No failure path lies between
     * the two calls, so no rollback is needed. */
    runloom_mn_pending_inc(h);
    runloom_mn_hub_submit(h, g);
    return 0;
}

int runloom_mn_fiber_c(runloom_c_entry_fn fn, void *arg)
{
    if (fn == NULL) { errno = EINVAL; return -1; }
    return runloom_mn_fiber_core(NULL, fn, arg, 0, -1, -1);
}

PyObject *runloom_mn_fiber(PyObject *callable, size_t stack_size)
{
    if (runloom_mn_fiber_core(callable, NULL, NULL, stack_size, -1, -1) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

/* Spawn `callable` on a RESERVED OFFLOAD HUB -- the entry point for running a
 * blocking call as an ordinary fiber (offload hubs; see the block in
 * mn_sched.c for why a dedicated hub rather than a general one).
 *
 * Round-robins across the K offload hubs, so K of them carry K concurrent
 * blocking calls.  Everything after placement is the normal spawn path, which
 * is the entire point: submit, completion, wake and GC-visibility
 * (runloom_greg_link) are the same tested code every other fiber uses.
 *
 * Raises RuntimeError when no offload hubs are reserved, rather than quietly
 * falling back to a general hub: running a blocking call there would strand
 * every g woken on that hub for the duration, which is the failure this whole
 * mechanism exists to avoid.  The caller decides what to do instead. */
PyObject *runloom_mn_offload_fiber(PyObject *callable, size_t stack_size)
{
    int k = __atomic_load_n(&runloom_offload_hub_count, __ATOMIC_RELAXED);
    unsigned long slot;
    int hub;
    if (runloom_hubs == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "runloom_mn_init() must be called first");
        return NULL;
    }
    if (k <= 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "stackweave: no offload hubs reserved "
                        "(pass offload_hubs=K to mn_init / stackweave.run)");
        return NULL;
    }
    slot = (unsigned long)__atomic_fetch_add(&runloom_mn_offload_rr, 1, __ATOMIC_RELAXED);
    hub  = runloom_general_hub_count() + (int)(slot % (unsigned long)k);
    if (runloom_mn_fiber_core(callable, NULL, NULL, stack_size, -1, hub) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

/* Spawn `callable` on hub `hub_id` and keep it there (see mn_sched.h).
 * hub_id < 0 raises ValueError. */
PyObject *runloom_mn_fiber_pinned(PyObject *callable, size_t stack_size,
                                  int hub_id)
{

    if (hub_id < 0) {
        PyErr_Format(PyExc_ValueError,
                         "invalid hub_id: %d passed",
                         hub_id);
        return NULL;
    }

    /* Validate against the LIVE hub count.  The bound is runloom_hub_count,
     * DELIBERATELY including the reserved offload hubs: the one sanctioned
     * exception to "no general work ever lands on an offload hub" (CLAUDE.md).
     * Liveness only -- on a busy offload hub the fiber strands; nothing
     * migrates, so never corruption. */
    if (runloom_hubs == NULL || runloom_hub_count <= 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "mn_fiber(hub=): the M:N runtime is not running");
        return NULL;
    }
    if (hub_id >= runloom_hub_count) {
        PyErr_Format(PyExc_ValueError,
                     "mn_fiber(hub=%d): only %d hub%s running (0..%d)",
                     hub_id, runloom_hub_count,
                     runloom_hub_count == 1 ? " is" : "s are",
                     runloom_hub_count - 1);
        return NULL;
    }
    if (runloom_mn_fiber_core(callable, NULL, NULL, stack_size, -1,
                              hub_id) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

/* Bulk-spawn: create `n` fibers, ALL running the same Python `callable`,
 * by looping the spawn core in C.  This removes the n Python->C call dispatches
 * + per-call PyArg parsing that a Python `for i: mn_fiber(fn)` loop pays -- the
 * fibers still run the Python callable (each gets its own g + tstate slot
 * and invokes `callable()` when scheduled).  Returns 0 on success, or -1 with a
 * Python error set if a spawn fails partway (the already-created fibers are
 * live and will run; the caller decides whether to treat a partial as fatal).
 * Must be called from inside the M:N runtime (root or a fiber). */
int runloom_mn_fiber_n(PyObject *callable, long n, size_t stack_size, int indexed)
{
    long i;
    if (runloom_hubs == NULL) {
        PyErr_SetString(PyExc_RuntimeError,
                        "runloom_mn_init() must be called first");
        return -1;
    }
    for (i = 0; i < n; i++) {
        if (runloom_mn_fiber_core(callable, NULL, NULL, stack_size,
                               indexed ? i : -1, -1) < 0) {
            return -1;
        }
    }
    return 0;
}

/* ---- M:N deadlock census (Go's checkdead analogue) -------------------------
 *
 * runloom_mn_run() polls the per-hub pending sum and returns when it reaches
 * 0 (every fiber finished).  If the count instead stays > 0 while the WHOLE
 * system is quiescent -- every hub idle, no runnable / stealable work anywhere,
 * no sleeper or timer due, and nothing in flight on netpoll / blockpool /
 * io_uring / a foreign park -- then the remaining fibers are blocked on a
 * channel / lock / await with no event that can ever wake them.  That is Go's
 * "all goroutines are asleep - deadlock!".  Without this census mn_run would
 * spin the 1 ms poll forever: a silent hang with no diagnostic.  This is the
 * M:N analogue of the single-thread detector at drain exit (which self-disables
 * once hubs exist -- runloom_sched_drain.c.inc). */

/* True if any source could still wake a parked fiber.  Reads only process-
 * global + per-hub atomics (no GIL / tstate), exactly like the sysmon watchdog.
 * A racy read is fine: the verdict requires this to stay false for many
 * consecutive polls, and a real wake (microseconds) resets the streak. */
static int runloom_mn_has_wakeable_work(void)
{
    int i;
    /* Cross-thread / external wake sources: an fd that may become readable, an
     * executor or io_uring completion, or a foreign (non-fiber) thread parked
     * on a runloom primitive that another thread will release.  Any of these
     * can wake a parked fiber -> not a deadlock (Go's checkdead likewise
     * excludes netpollWaiters and syscall/cgo threads). */
    /* mn-sim refinement (MN_SIM_DST_PLAN.md I5): under the armed sim census a
     * netpoll-parked fiber is wakeable ONLY by the ledger or a logical
     * deadline -- with both empty it is settle-reap fodder, not wakeable work,
     * and counting it here would hold the deadlock census open on parkers the
     * reap is about to cancel. */
    if (runloom_netpoll_parked_count() != 0) {
        if (!(runloom_sim_enabled() && runloom_mn_ctrl_armed()) ||
            runloom_sim_ready_peek_ns() >= 0 ||
            runloom_netpoll_deadline_peek_ns() >= 0)
            return 1;
        /* I5 review: only HUB-pool parkers are settle-reap fodder.  A
         * DEFAULT-pool parker (the coexisting legacy plane) is woken by the
         * H=1 pump, which the census can neither see nor reap -- classifying
         * it unwakeable produced a false DEADLOCK verdict (review repro). */
        if (runloom_netpoll_parked_count() !=
            runloom_netpoll_parked_count_hub_pools())
            return 1;
    }
    if (runloom_blockpool_inflight()    != 0) return 1;
    if (runloom_iouring_inflight()      != 0) return 1;
    if (runloom_foreign_park_inflight() != 0) return 1;
    if (__atomic_load_n(&runloom_global_runq_len, __ATOMIC_ACQUIRE) != 0) return 1;
    for (i = 0; i < runloom_hub_count; i++) {
        runloom_hub_t *h = &runloom_hubs[i];
        /* Mid-resume: the hub is running a fiber right now. */
        if (__atomic_load_n(&h->resume_start_ns, __ATOMIC_RELAXED) != 0) return 1;
        /* Runnable work queued on this hub: ready ring / steal deque / the
         * mn_fiber + wake_g submission list. */
        if (!runloom_sched_ready_empty(&h->sched))                  return 1;
        if (runloom_cldeque_size(&h->deque) != 0)                   return 1;
        /* Lock-free ACQUIRE read of the submission list head, paired with RELEASE
         * stores at the 6 sub_head WRITE sites (mn_api / hub_main / hub_resume /
         * init): a publish (store g) is RELEASE so this read sees a non-NULL head
         * with g's fields visible; a clear (store NULL) is RELAXED.  Closes the
         * tools/lifefuzz TSan race (hub_main:273); tools/README Finding D #4. */
        if (__atomic_load_n(&h->sub_head, __ATOMIC_ACQUIRE) != NULL) return 1;
        /* A sleeper / timer that will fire and wake a fiber.  RELAXED-atomic: the
         * owning hub writes these non-atomically (sleep_push/pop, timer_push/pop),
         * so this cross-thread read is the lone plain access otherwise -- match the
         * atomic siblings above (the ready_head/tail fix in runloom_sched_ready_empty). */
        if (__atomic_load_n(&h->sched.sleep_size, __ATOMIC_RELAXED) != 0) return 1;
        if (__atomic_load_n(&h->sched.timer_size, __ATOMIC_RELAXED) != 0) return 1;
    }
    return 0;
}

/* For the mn_run progress-stall watchdog: TRUE iff some hub has a QUEUED runnable
 * fiber (ready ring / steal deque / submission list / global runq) AND no hub is
 * currently resuming one.  That is the stranded-runnable signature -- work that
 * should be executing but nothing is running it and nothing is completing.  It
 * deliberately EXCLUDES fibers parked on I/O / timers / foreign waits (a
 * legitimately idle server must NOT be kicked) and the case where a hub is
 * mid-resume (a long fiber is genuinely executing, not stranded). */
static int runloom_mn_stalled_with_runnable(void)
{
    int i, runnable = 0;
    for (i = 0; i < runloom_hub_count; i++) {
        runloom_hub_t *h = &runloom_hubs[i];
        if (__atomic_load_n(&h->resume_start_ns, __ATOMIC_RELAXED) != 0)
            return 0;                                   /* a fiber is executing */
        if (!runnable &&
            (!runloom_sched_ready_empty(&h->sched) ||
             runloom_cldeque_size(&h->deque) != 0 ||
             __atomic_load_n(&h->sub_head, __ATOMIC_ACQUIRE) != NULL))
            runnable = 1;
    }
    if (!runnable)
        runnable = (__atomic_load_n(&runloom_global_runq_len, __ATOMIC_ACQUIRE) != 0);
    return runnable;
}

/* Wake every hub out of WHATEVER wait it is in so it re-polls its submission list
 * + neighbours.  A recovery nudge for a fiber made runnable whose hub missed the
 * wakeup; harmless when there is nothing to find (the hub re-checks and re-parks).
 * It MUST mirror the full wake routing in runloom_mn_hub_submit, because a hub can
 * be blocked two different ways and the idle condvar reaches only one: a netpoll
 * hub blocks in epoll_wait / the pump (woken by the pump eventfd), and only a bare
 * idle hub waits on idle_cond. */
static void runloom_mn_kick_all_hubs(void)
{
    int i;
    for (i = 0; i < runloom_hub_count; i++) {
        runloom_hub_t *h = &runloom_hubs[i];
        RUNLOOM_RLOCK(&h->idle_lock, RUNLOOM_RANK_HUB_IDLE);
        runloom_cond_signal(&h->idle_cond);               /* idle-condvar hub */
        RUNLOOM_RUNLOCK(&h->idle_lock, RUNLOOM_RANK_HUB_IDLE);
        runloom_netpoll_wake_pump((void *)h);             /* pump / epoll hub */
    }
}

/* RUNLOOM_DEADLOCK_MS: consecutive-quiescent-millisecond budget mn_run requires
 * before declaring a deadlock (0 disables the census).  Generous default so a
 * brief all-hubs-idle window -- e.g. while a foreign thread computes a value it
 * will send into a channel -- does not trip it; a genuine deadlock stays
 * quiescent forever.  Read once. */
static long runloom_mn_deadlock_ms(void)
{
    static long v = -1;
    long cur = __atomic_load_n(&v, __ATOMIC_RELAXED);
    if (cur < 0) {
        const char *e = getenv("STACKWEAVE_DEADLOCK_MS");
        cur = (e != NULL && *e != '\0') ? atol(e) : 200;
        if (cur < 0) cur = 0;
        __atomic_store_n(&v, cur, __ATOMIC_RELAXED);
    }
    return cur;
}

/* RUNLOOM_STALL_KICK_MS: consecutive no-progress 1ms polls mn_run tolerates before
 * kicking every hub awake as a liveness backstop (0 disables).  Default 4: a busy
 * run's pending changes each poll so it never triggers; a genuine stranded-runnable
 * stall recovers in a few ms instead of hanging.  Read once. */
static long runloom_mn_stall_kick_polls(void)
{
    static long v = -1;
    long cur = __atomic_load_n(&v, __ATOMIC_RELAXED);
    if (cur < 0) {
        const char *e = getenv("STACKWEAVE_STALL_KICK_MS");
        cur = (e != NULL && *e != '\0') ? atol(e) : 4;
        if (cur < 0) cur = 0;
        __atomic_store_n(&v, cur, __ATOMIC_RELAXED);
    }
    return cur;
}

Py_ssize_t runloom_mn_run(void)
{
    int i;
    /* Py_ssize_t to match the per-hub completed counter. */
    Py_ssize_t total_completed = 0;
    PyThreadState *saved;
    /* Controlled-replay: all pre-run mn_fiber placement is done; release the hubs'
     * startup gate so their first census is over the complete requester set. */
    runloom_mn_ctrl_arm();
    saved = PyEval_SaveThread();
    {
        long thresh   = runloom_mn_deadlock_ms();   /* quiescent-ms budget; 0 = off */
        long quiet    = 0;                          /* consecutive quiescent polls */
        int  kicked   = 0;                          /* lost-wakeup re-kick fired? */
        int  reported = 0;                          /* diagnostic already dumped? */
        int  raise    = 0;                          /* deadlock mode 2 (raise) verdict */
        int  sig_raised = 0;                        /* a Python signal handler raised */
        unsigned sigpoll = 0;                       /* throttle the signal service */
        long last_total  = -1;                      /* stall watchdog: last pending seen */
        long stall       = 0;                       /* consecutive no-progress polls */
        long stall_kick  = runloom_mn_stall_kick_polls();  /* flat polls before kick; 0=off */
        for (;;) {
            /* Sum the per-hub pending fields (no global counter -- that was one
             * contended cache line that serialized parallel spawn).  This is
             * race-free for the termination test because (1) the steal helper now
             * bumps the thief BEFORE dropping the victim, so a stolen g is
             * transiently OVER-counted, never under-counted -> the sum never
             * momentarily reads 0 with a stolen g in flight; (2) any spawner is
             * itself a live counted g, so a concurrent spawn can't drive the sum
             * to 0.  ACQUIRE loads pair with the RELEASE spawn/complete writes.
             * Read only at the ~1ms poll cadence, so the O(hubs) sum is off the
             * hot path. */
            long total = 0;
            {
                int hi;
                for (hi = 0; hi < runloom_hub_count; hi++)
                    total += __atomic_load_n(&runloom_hubs[hi].pending,
                                             __ATOMIC_ACQUIRE);
            }
            /* == 0, not <= 0: the sum equals spawns-minus-completes and is never
             * truly negative, but if read-skew ever produced a transient
             * negative, polling again is the safe response -- a missed-exit
             * (brief extra poll) beats a false exit (use-after-free). */
            if (total == 0) break;

            /* Progress-stall liveness backstop.  If pending has not moved for
             * stall_kick consecutive 1ms polls WHILE a queued runnable fiber sits
             * un-run and no hub is resuming, that fiber is stranded on a hub that
             * missed its wake -- kick EVERY hub in EVERY wait mode to force a
             * re-drain.  Independent of the deadlock census below (which only fires
             * when has_wakeable_work() is FALSE): a stranded-runnable hang keeps
             * has_wakeable_work() TRUE via the I/O-parked fibers, so the census
             * never reaches its own kick.  runloom_mn_stalled_with_runnable()
             * excludes legitimate idle (all fibers parked on I/O) and a running
             * long fiber, so a correctly-idle server is never kicked. */
            if (total == last_total && stall_kick > 0 &&
                runloom_mn_stalled_with_runnable()) {
                if (++stall >= stall_kick) {
                    runloom_mn_kick_all_hubs();
                    stall = 0;
                }
            } else {
                last_total = total;
                stall = 0;
            }

            if (thresh > 0 && runloom_deadlock_mode() != 0 &&
                !runloom_mn_has_wakeable_work()) {
                quiet++;
                /* Partway through the stall, re-kick the idle hubs once: if a
                 * fiber was made runnable but its hub missed the wakeup, this
                 * recovers it and the next poll resets the streak. */
                if (!kicked && quiet == thresh / 4 + 1) {
                    runloom_mn_kick_all_hubs();
                    kicked = 1;
                }
                if (quiet >= thresh) {
                    /* Stable quiescence with live fibers and no wake source,
                     * even after a re-kick: deadlock (or, if none are parked on
                     * a channel / park_safe, a lost wakeup / accounting leak). */
                    long ndead = runloom_count_deadlockable_fibers(NULL);
                    if (!reported) {
                        if (ndead > 0)
                            fprintf(stderr,
                                "\nrunloom: DEADLOCK -- %ld fiber(s) blocked on a "
                                "channel/lock/await with every hub idle and no I/O, "
                                "timer, or runnable work left to wake them:\n", ndead);
                        else
                            fprintf(stderr,
                                "\nrunloom: STALL -- %ld live fiber(s) but the M:N "
                                "scheduler is quiescent with no wake source "
                                "(possible lost wakeup):\n", total);
                        fflush(stderr);
                        runloom_dump_fibers_fd(2);
                        reported = 1;
                    }
                    if (runloom_deadlock_mode() == 2) { raise = 1; break; }
                    /* warn (default): do NOT abandon the fibers -- keep polling
                     * so a correct program whose quiescence was a false alarm
                     * (e.g. a slow foreign feeder) still resumes.  The
                     * diagnostic has been surfaced; a real deadlock now hangs
                     * VISIBLY instead of silently. */
                }
            } else {
                quiet = 0; kicked = 0; reported = 0;
            }
            runloom_sleep_ns(1000000LL);   /* 1 ms poll */
            /* Service Python signal handlers on the MAIN thread (~every 16 ms).
             * On free-threaded 3.13t ONLY the main thread runs Python signal
             * handlers, and mn_run otherwise polls with the main tstate detached
             * (PyEval_SaveThread above) -- so a SIGINT/SIGALRM during an M:N run
             * was deferred until every fiber finished, i.e. Ctrl-C could not
             * interrupt a server run (audit finding B5).  Briefly re-attach and
             * run pending handlers; if one raised, carry it out of mn_run.  The
             * hub fibers' wait_fd parkers are owned by the HUB scheds, not this
             * main sched, so runloom_netpoll_signal_wake (keyed on the caller's
             * sched) finds none eligible from here -- the documented
             * no-eligible-parker fallback is exactly "carry it out of run()".
             *
             * SIGNALS NOW REACH HUB FIBERS -- but only those parked on an fd.
             * The sigpoll above offers a raised handler to a parked fiber via
             * runloom_netpoll_signal_wake before falling back to carry-out, so
             * an `except KeyboardInterrupt:` / `finally:` inside a hub fiber
             * runs, matching the single-thread plane.  The wake arm for hub
             * parkers (runloom_mn_wake_g) had always been there; only the
             * owner filter, keyed on the CALLING sched, kept it unreachable.
             *
             * The main thread reserves a slot on the TARGET hub's sched before
             * claiming the parker, never after: claiming first and then finding
             * the slot busy would leave a parker CAS'd to WOKEN with nobody
             * delivering, and the fiber would park forever (the bug fixed in
             * 8af0d807, much easier to reintroduce here).  Every access to
             * signal_exc is atomic because it is now written by one thread and
             * read by another.
             *
             * ALL THREE RECIPIENT CLASSES ARE NOW REACHABLE FROM HERE, and
             * the two that were not both needed a different STRUCTURE rather
             * than a different filter -- each now registers a stack node in a
             * process-wide list that this thread can walk:
             *   - io_uring waiters (runloom_iou_sigwaiters, once it stopped
             *     being RUNLOOM_TLS).  Note the wake must route by how the
             *     waiter parked: a hub parker used park_current, which
             *     wake_safe cannot observe, so it takes mn_wake_g.
             *   - sched_sleep_io sleepers, select.poll's reprobe
             *     (runloom_sleep_sigwaiters).  Nothing here touches a hub's
             *     sleep heap; the sleeper removes ITSELF on its own thread.
             *     Its one hazard is that the hub's timer pop is a second
             *     scheduler for the same g -- see g->sleep_claimed.
             * The fallback below stays exactly as it was and is what keeps
             * NEVER FETCH A SIGNAL YOU CANNOT DELIVER true here: each arm
             * returns 0 when it has no recipient, and carry-out is always a
             * valid delivery, so nothing is ever held waiting for one.
             * tests/test_signal_recipient.py asserts all three.
             *
             * The still-running hubs are stopped by the caller's mn_fini, the
             * same as the deadlock-raise early-return path below. */
            if ((++sigpoll & 0xfu) == 0) {
                PyEval_RestoreThread(saved);
                if (PyErr_CheckSignals() < 0) {
                    /* A handler raised.  Offer it to a fiber parked in a
                     * cooperative call on ANY hub first, so its own
                     * try/except/finally runs -- the same contract the
                     * single-thread plane provides.  Only if no hub fiber is
                     * parked do we fall back to carrying it out of mn_run,
                     * which is what this loop did unconditionally before. */
                    PyObject *st = NULL, *sv = NULL, *sb = NULL;
                    PyErr_Fetch(&st, &sv, &sb);
                    PyErr_NormalizeException(&st, &sv, &sb);
                    if (sv != NULL && sb != NULL) PyException_SetTraceback(sv, sb);
                    if (sv != NULL && (runloom_netpoll_signal_wake(sv) ||
                                       runloom_iouring_signal_wake(sv) ||
                                       runloom_sched_sleep_signal_wake(sv))) {
                        Py_XDECREF(st); Py_XDECREF(sb);
                        saved = PyEval_SaveThread();
                        continue;     /* delivered; the hub fiber raises it */
                    }
                    PyErr_Restore(st, sv, sb);
                    sig_raised = 1;
                    break;
                }
                saved = PyEval_SaveThread();
            }
        }
        /* Silence the unused-variable warning that the per-hub loop used
         * to consume. */
        (void)i;
        /* sig_raised broke the loop with the main tstate ALREADY attached and the
         * handler's exception set -- carry it straight out (don't double-attach). */
        if (sig_raised) return -1;
        PyEval_RestoreThread(saved);
        if (raise) {
            long ndead = runloom_count_deadlockable_fibers(NULL);
            PyErr_Format(PyExc_RuntimeError,
                "stackweave: deadlock -- %ld fiber(s) blocked on a channel/lock/await "
                "with no way to wake (deadlock mode: raise)",
                ndead > 0 ? ndead : 0);
            return -1;
        }
    }
    for (i = 0; i < runloom_hub_count; i++) {
        total_completed += __atomic_load_n(&runloom_hubs[i].sched.completed,
                                           __ATOMIC_ACQUIRE);
    }
    return total_completed;
}
