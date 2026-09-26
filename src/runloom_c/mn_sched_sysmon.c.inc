/* Part of mn_sched.c -- #included into that translation unit, not compiled
 * standalone.  See mn_sched.c for the includes/typedefs/file-scope state
 * these definitions depend on. */


/* Called from the fatal-signal crash handler (runloom_crash.c) on the faulting
 * hub thread, the instant it becomes the crash owner.  Stops the sysmon
 * watchdog loop so it never marks this hub a recoverable wedge mid-dump, and
 * disables preemption so the faulting g stays put -- the handler's chain-out
 * re-faults and cores cleanly.  Async-signal-safe: just stores to the loop-stop
 * flag + the preempt enable flag (no locks, no malloc, no Python C-API). */
void runloom_sched_freeze_for_crash(void)
{
    __atomic_store_n(&runloom_sysmon_stop, 1, __ATOMIC_RELEASE);
    runloom_preempt_enabled = 0;
}

/* ---- Reading the RUNNING tstate's attach state from the sysmon thread ----
 *
 * Under migration each fiber runs on its OWN PyThreadState, and the hub's
 * tstate is detached for the whole resume, so the hub's attach state says
 * nothing about the fiber (it is what stood preemption down: "DETACHED" for
 * every CPU-bound fiber).  The hub publishes the tstate its resume runs on in
 * h->resume_tstate; but a per-g tstate is FREED when its fiber completes, so
 * sysmon cannot simply dereference a pointer it loaded a moment ago.
 *
 * Hazard pointer (one reader, the sysmon thread): sysmon stores the pointer
 * it is about to read into runloom_sysmon_hazard_ts, fences, and re-checks
 * that the hub still publishes it; the deleter (runloom_g_decref, any thread)
 * fences and spins while the hazard names its tstate.  Store/fence/load on
 * both sides is the Dekker pair: if sysmon still sees the tstate published,
 * the deleter sees the hazard and waits; if the deleter saw no hazard, sysmon
 * sees the cleared slot and does not touch the tstate. */
static void *volatile runloom_sysmon_hazard_ts = NULL;
/* Serialises the hazard's WRITERS: the sysmon thread and any hubs() caller
 * (mn_sched_hubinfo.c.inc).  The deleter only reads it. */
static runloom_mutex_t runloom_sysmon_hazard_lock;
static int             runloom_sysmon_hazard_lock_ready = 0;  /* init in runloom_sysmon_config */

void runloom_sysmon_tstate_retire_wait(PyThreadState *ts)
{
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    while (__atomic_load_n(&runloom_sysmon_hazard_ts, __ATOMIC_ACQUIRE) == (void *)ts)
        runloom_sleep_ns(1000);          /* sysmon holds it for a few loads */
}

/* Attach state of what hub `h` is running right now: the fiber's tstate under
 * migration, the hub's own otherwise; falls back to the hub's tstate when the
 * hub is idle.  -1 when nothing could be read safely. */
int runloom_sysmon_hub_attach_state(runloom_hub_t *h, PyThreadState *hts)
{
    void *ts = __atomic_load_n((void **)&h->resume_tstate, __ATOMIC_ACQUIRE);
    int tss;
    if (ts == NULL) return runloom_tstate_attach_state(hts);
    runloom_mutex_lock(&runloom_sysmon_hazard_lock);
    __atomic_store_n(&runloom_sysmon_hazard_ts, ts, __ATOMIC_RELEASE);
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    if (__atomic_load_n((void **)&h->resume_tstate, __ATOMIC_ACQUIRE) != ts) {
        tss = -1;                        /* resume ended under us; try next tick */
    } else {
        tss = runloom_tstate_attach_state((PyThreadState *)ts);
    }
    __atomic_store_n(&runloom_sysmon_hazard_ts, (void *)NULL, __ATOMIC_RELEASE);
    runloom_mutex_unlock(&runloom_sysmon_hazard_lock);
    return tss;
}

/* sysmon watchdog thread.  Holds NO GIL and NO tstate -- it only reads
 * per-hub atomics and writes to stderr, never touching the Python C-API.
 * Scans every runloom_sysmon_tick_ns; logs a hub whose current runloom_coro_resume
 * has run longer than runloom_sysmon_wedge_ns (an unanticipated non-yielding
 * block), once per wedge episode, and a matching "recovered" line so a run's
 * wedge count + dwell are measurable.  It also arms ATTACHED-wedge preemption
 * (a DETACHED wedge needs none -- work-stealing drains its fresh fibers). */
static RUNLOOM_THREAD_RET runloom_sysmon_main(void *arg)
{
    int n = runloom_hub_count;
    /* w_seq[i] = the resume_seq currently logged as wedged on hub i (0 = none);
     * w_start[i] = its resume_start_ns, for the recovery-dwell report. */
    long      *w_seq    = (long *)calloc((size_t)(n > 0 ? n : 1), sizeof(long));
    long long *w_start  = (long long *)calloc((size_t)(n > 0 ? n : 1),
                                             sizeof(long long));
    (void)arg;
    if (w_seq == NULL || w_start == NULL) {
        free(w_seq); free(w_start);
        RUNLOOM_THREAD_RETURN(NULL);
    }
    long idle_streak = 0;   /* consecutive ticks with NO hub resuming a fiber */
    while (!__atomic_load_n(&runloom_sysmon_stop, __ATOMIC_ACQUIRE)) {
        long long now = runloom_monotonic_ns();
        int i;
        int any_busy = 0;   /* a hub is mid-resume this tick -> a wedge can form */
        for (i = 0; i < n; i++) {
            runloom_hub_t *h = &runloom_hubs[i];
            long long start = __atomic_load_n(&h->resume_start_ns,
                                              __ATOMIC_RELAXED);
            if (start != 0) any_busy = 1;
            long seq = __atomic_load_n(&h->resume_seq, __ATOMIC_RELAXED);
            /* Acquire load: the hub publishes its tstate on its own thread; a
             * racy/NULL read here is fine -- attach-state -1 classifies as "?"
             * and gates the preempt dispatch closed. */
            PyThreadState *hts = (PyThreadState *)__atomic_load_n((void **)&h->tstate, __ATOMIC_ACQUIRE);
            int tss = runloom_sysmon_hub_attach_state(h, hts);
            /* End of a previously-reported episode: the hub moved to a new
             * resume (seq changed) or finished resuming (start cleared). */
            if (w_seq[i] != 0 && (seq != w_seq[i] || start == 0)) {
                long pend = __atomic_load_n(&h->pending, __ATOMIC_RELAXED);
                if (!runloom_sysmon_quiet)
                    fprintf(stderr,
                        "[STACKWEAVE_SYSMON] hub %d RECOVERED after ~%.1f ms "
                        "(pending=%ld)\n",
                        i, (double)(now - w_start[i]) / 1e6, pend);
                w_seq[i] = 0;
            }
            /* Start of a wedge episode: this resume has overrun the budget and
             * we have not logged THIS seq yet. */
            if (start != 0 && (now - start) > runloom_sysmon_wedge_ns &&
                w_seq[i] != seq) {
                long pend = __atomic_load_n(&h->pending, __ATOMIC_RELAXED);
                /* Classify by the hub tstate's attach state (racy read, fine
                 * for a diagnostic): DETACHED => the wedged g released the
                 * tstate (blocking IO); work-stealing already drains its fresh
                 * fibers to idle hubs.  ATTACHED => CPU-bound or a raw
                 * tstate-holding syscall -> needs preemption.  SUSPENDED =>
                 * stop-the-world. */
                const char *cls =
                    runloom_hub_is_offload(i)   ? "OFFLOAD HUB (blocking on purpose -- not a wedge)" :
                    tss == RUNLOOM_TS_DETACHED  ? "DETACHED (blocking-IO)" :
                    tss == RUNLOOM_TS_ATTACHED  ? "ATTACHED (CPU/raw-syscall: needs preemption)" :
                    tss == RUNLOOM_TS_SUSPENDED ? "SUSPENDED (stop-the-world)" :
                                               "?";
                if (!runloom_sysmon_quiet)
                    fprintf(stderr,
                        "[STACKWEAVE_SYSMON] hub %d WEDGED %.1f ms in g=%p tstate=%s -- "
                        "%ld g(s) stranded on this hub\n",
                        i, (double)(now - start) / 1e6,
                        (void *)__atomic_load_n((void **)&h->resume_g, __ATOMIC_RELAXED), cls,
                        pend > 0 ? pend - 1 : 0);
                w_seq[i]   = seq;
                w_start[i] = start;
            }
            /* Preemption dispatch.  An ATTACHED wedge holds its tstate -> ask
             * the eval-frame wrapper to yield the running g at its next Python
             * frame.  Re-armed every tick the wedge persists (the wrapper clears
             * it on each yield), giving periodic time-slicing.  (A DETACHED wedge
             * needs no dispatch: work-stealing already drains its fresh fibers to
             * idle hubs.) */
            /* EXCLUSION 3 of 3 (RUNLOOM_OFFLOAD_HUBS, mn_sched.c): an offload
             * hub blocks ON PURPOSE and must never be preempted for it.
             * A blocking-IO offload is DETACHED and the tss test below already
             * skips it -- but `offload` also sanctions CPU-bound work (hashing,
             * compression), which runs ATTACHED and would otherwise be yielded
             * every preempt tick, forever, to a scheduler with nothing else
             * runnable on that hub. */
            if (runloom_preempt_enabled && start != 0 &&
                (now - start) > runloom_sysmon_preempt_ns &&
                tss == RUNLOOM_TS_ATTACHED && !runloom_hub_is_offload(i)) {
                __atomic_store_n(&h->preempt_requested, 1, __ATOMIC_RELAXED);
                /* Single-frame liveness backstop: the eval-frame flag above only
                 * fires at a Python FRAME boundary, so it cannot break a
                 * single-frame `while: pass` (no calls -> no frame entry).  Post
                 * an eval-breaker pending call too; it rides backward-jump checks
                 * INSIDE the frame and yields the running g (flags=0 -> runs on
                 * the wedged HUB thread, not the parked main thread).  This is
                 * needed in the DEFAULT scheduler as well, not only the controlled
                 * one: at H=1 (or any time the single sibling shares the wedged
                 * hub) there is no parallel hub to make progress while a
                 * single-frame loop spins, so without this the sibling starves
                 * (test_preemption_busy_loop_yields_to_sibling at H=1 hung on a
                 * scheduling order that ran the busy loop first).  Frame-bearing
                 * code is already broken off by the wrapper above; the extra
                 * pending call there only yields a g that is anyway about to
                 * yield, which is harmless. */
#ifdef RUNLOOM_MN_LIVENESS_BACKSTOP
                /* Skipped under the armed controlled scheduler (MN_SIM_DST_PLAN
                 * I2 gate [7]): this wall-clock pending-call injects an
                 * unconditional yield at an OS-timing-decided frame -- exactly
                 * the nondeterministic escape the seed forbids; the
                 * deterministic frame-count preempt is the liveness story
                 * under the barrier. */
                if (runloom_mn_interp != NULL && !runloom_mn_ctrl_armed()) {
                    _PyEval_AddPendingCall(runloom_mn_interp,
                                           runloom_mn_liveness_pending_cb, NULL, 0);
                }
#endif
            }
        }
        /* Piggyback the depot auto-cap recompute on the watchdog tick: decay the
         * live-stack watermark, resize the pool cap.  Cheap, off the hot path. */
        runloom_stack_autocap_tick();
        /* Adaptive tick (Go's sysmon deep-sleep when idle): no hub is resuming
         * a fiber, so no wedge can form -- back off the scan to cut idle wakeups.
         * Reset to the base tick the instant a hub goes busy.  The backoff is
         * capped well under the wedge budget so a fresh wedge after the system
         * wakes is still caught within ~wedge_ns. */
        if (any_busy) {
            idle_streak = 0;
            runloom_sysmon_tick_ns = runloom_sysmon_tick_base_ns;
        } else if (idle_streak < 1024) {
            long long cap = runloom_sysmon_wedge_ns / 2;
            idle_streak++;
            runloom_sysmon_tick_ns = runloom_sysmon_tick_base_ns << (idle_streak < 3 ? (int)idle_streak : 3);
            if (cap > 0 && runloom_sysmon_tick_ns > cap) runloom_sysmon_tick_ns = cap;
        }
        runloom_sleep_ns(runloom_sysmon_tick_ns);
    }
    free(w_seq);
    free(w_start);
    RUNLOOM_THREAD_RETURN(NULL);
}

/* Interpret an OPT-OUT env flag for the stall-recovery features.  Explicit
 * "0" => off; explicit anything-else => on; UNSET => on.  (Subject to the
 * per-g gates below.) */
static int runloom_flag_default_on(const char *v)
{
    if (v != NULL) return (v[0] != '0') ? 1 : 0;
    return 1;
}

/* Read RUNLOOM_SYSMON / RUNLOOM_SYSMON_MS once and set the enable flag + threshold.
 * Must run in mn_init BEFORE the hub threads start so the per-resume
 * instrumentation is live from the first resume.  RUNLOOM_PREEMPT defaults ON
 * (opt out with =0); it forces the sysmon thread on but NOT
 * its stderr logging -- WEDGED/RECOVERED lines print only when the user opts in
 * via RUNLOOM_SYSMON=1 (or RUNLOOM_SYSMON_QUIET=0). */
static void runloom_sysmon_config(void)
{
    const char *e = getenv("STACKWEAVE_SYSMON");
    const char *q = getenv("STACKWEAVE_SYSMON_QUIET");
    const char *ms;
    /* Explicit opt-in signal: the user set RUNLOOM_SYSMON to a truthy value to
     * WATCH sysmon.  Preemption (below) may also force the sysmon thread on, but
     * that alone must not produce the WEDGED/RECOVERED chatter on stderr. */
    int sysmon_explicit = (e != NULL && e[0] != '0');
    if (!runloom_sysmon_hazard_lock_ready) {      /* mn_init, single-threaded */
        runloom_mutex_init(&runloom_sysmon_hazard_lock);
        runloom_sysmon_hazard_lock_ready = 1;
    }
    runloom_sysmon_enabled = sysmon_explicit ? 1 : 0;
    /* Quiet by default: log only when sysmon was explicitly requested.  When it
     * runs solely to service preemption (the common default), stay
     * silent.  RUNLOOM_SYSMON_QUIET still forces the choice either way
     * (=1 quiet, =0 verbose). */
    if (q != NULL) runloom_sysmon_quiet = (q[0] != '0') ? 1 : 0;
    else           runloom_sysmon_quiet = sysmon_explicit ? 0 : 1;
    /* RUNLOOM_PREEMPT: ATTACHED/CPU preemption (uses the free-threaded attach
     * states to classify the wedge).  It forces the sysmon instrumentation +
     * watchdog on (that is what detects the wedge and arms preempt_requested).
     * The eval-frame wrapper is installed in mn_init. */
    runloom_preempt_enabled = runloom_flag_default_on(getenv("STACKWEAVE_PREEMPT"));  /* default ON */
    /* Under per-g-tstate (migration) the wedge test reads the RUNNING fiber's
     * tstate through h->resume_tstate (runloom_sysmon_hub_attach_state), so a
     * CPU-bound fiber is ATTACHED-wedged exactly as on the per-hub scheduler
     * and preemption stays on. */
    if (runloom_preempt_enabled) runloom_sysmon_enabled = 1;
    if (!runloom_sysmon_enabled) return;
    ms = getenv("STACKWEAVE_SYSMON_MS");
    if (ms != NULL) {
        long long v = atoll(ms);
        if (v > 0) runloom_sysmon_wedge_ns = v * 1000000LL;
    }
    /* ATTACHED-preempt threshold: defaults to the wedge budget; RUNLOOM_PREEMPT_MS
     * tunes the CPU time-slice independently of the detection budget. */
    runloom_sysmon_preempt_ns = runloom_sysmon_wedge_ns;
    {
        const char *pm = getenv("STACKWEAVE_PREEMPT_MS");
        if (pm != NULL) {
            long long v = atoll(pm);
            if (v > 0) runloom_sysmon_preempt_ns = v * 1000000LL;
        }
    }
    /* Scan ~5x faster than the wedge budget (cap 10 ms) so detection latency
     * is a small fraction of the threshold without busy-spinning. */
    runloom_sysmon_tick_ns = runloom_sysmon_wedge_ns / 5;
    if (runloom_sysmon_tick_ns > 10000000LL) runloom_sysmon_tick_ns = 10000000LL;
    if (runloom_sysmon_tick_ns < 1000000LL)  runloom_sysmon_tick_ns = 1000000LL;
    runloom_sysmon_tick_base_ns = runloom_sysmon_tick_ns;
}

/* Spawn the watchdog thread (after hubs exist).  Enabled-gated; a spawn
 * failure is non-fatal -- the scheduler runs fine without the watchdog. */
static void runloom_sysmon_spawn(void)
{
    if (!runloom_sysmon_enabled) return;
    runloom_sysmon_stop = 0;
    if (runloom_thread_create(&runloom_sysmon_thread, runloom_sysmon_main, NULL) == 0) {
        runloom_sysmon_running = 1;
    } else {
        fprintf(stderr, "[STACKWEAVE_SYSMON] watchdog thread spawn failed; "
                        "stall detection disabled\n");
    }
}

/* Stop + join the watchdog.  Called at the top of mn_fini, BEFORE the hubs
 * are torn down, so the scan never reads freed hub state. */
static void runloom_sysmon_stop_join(void)
{
    if (!runloom_sysmon_running) return;
    __atomic_store_n(&runloom_sysmon_stop, 1, __ATOMIC_RELEASE);
    runloom_thread_join(runloom_sysmon_thread);
    runloom_sysmon_running = 0;
}
