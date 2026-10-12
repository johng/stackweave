/* Part of mn_sched.c -- #included into that TU.  Per-hub diagnostic snapshot
 * for runloom.inspect.hubs(); see the runloom_hub_info_t contract in mn_sched.h.
 *
 * The interesting part is `blocked_at` -- the Python call site of a fiber
 * wedged inside a non-cooperative blocking C call.  Reading another execution
 * context's top frame from the snapshotting thread is a use-after-free in
 * general (frames mutate as code runs), so we restrict it to the ONE
 * configuration where it is safe by construction MINUS a documented, vanishing
 * residual:
 *
 *   - the fiber the hub is running must have overrun the wedge budget with its
 *     OWN tstate DETACHED: it did Py_BEGIN_ALLOW_THREADS and is now parked in a
 *     blocking syscall.  Its thread will NOT touch current_frame until
 *     END_ALLOW_THREADS re-attaches, so the frame chain is pinned for the
 *     duration of a genuine wedge, and that thread is the only one that can
 *     mutate it (a running fiber cannot be stolen; work-stealing only touches
 *     queued gs).  The hub's own tstate is no help: it is detached for every
 *     resume, and the fiber's frames are on the fiber's tstate.
 *   - the fiber's tstate is read through h->resume_tstate under the sysmon
 *     hazard pointer (runloom_sysmon_hub_attach_state), so a fiber that
 *     completes meanwhile cannot free it under the read.
 *
 * Residual: the fiber returning from its syscall mid-read re-attaches and could
 * overwrite the frame.  We re-confirm DETACHED immediately before the walk
 * (shrinking the window to ~the walk itself) and read only the TOP frame.  For a
 * genuinely-stuck fiber -- the use case -- it never returns, so the window is
 * zero.  This is opt-in best-effort; the always-safe full stack is
 * `py-spy dump --pid` (the stack_cmd runloom.inspect.hubs() emits). */

struct hubinfo_top_ctx {
    PyCodeObject *code;   /* new reference, or NULL */
    int           line;
};

/* runloom_iframe_walk callback: take the TOP frame's code object (a new
 * reference) and line, then stop the walk.  Allocates nothing: it runs under
 * the sysmon hazard (see hubinfo_top_code). */
static int hubinfo_top_cb(PyCodeObject *code, int line, void *cv)
{
    struct hubinfo_top_ctx *ctx = (struct hubinfo_top_ctx *)cv;
    Py_INCREF(code);
    ctx->code = code;
    ctx->line = line;
    return 1;   /* top frame only */
}

/* The top Python frame of the fiber hub `h` is running, if that fiber's tstate
 * is DETACHED: a new reference to its code object (NULL if none), and its line.
 * Read under the hazard runloom_sysmon_hub_attach_state uses, so the tstate
 * cannot be freed mid-read.  The deleter spins on the hazard without detaching,
 * so nothing that could allocate (or wait for a stop-the-world) runs while it
 * is held: the caller formats the code after this returns. */
static PyCodeObject *hubinfo_top_code(runloom_hub_t *h, int *line_out)
{
    struct hubinfo_top_ctx ctx;
    PyThreadState *ts =
        (PyThreadState *)__atomic_load_n((void **)&h->resume_tstate, __ATOMIC_ACQUIRE);
    ctx.code = NULL;
    ctx.line = 0;
    if (ts == NULL) return NULL;
    runloom_mutex_lock(&runloom_sysmon_hazard_lock);
    __atomic_store_n(&runloom_sysmon_hazard_ts, (void *)ts, __ATOMIC_RELEASE);
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    if (__atomic_load_n((void **)&h->resume_tstate, __ATOMIC_ACQUIRE) == (void *)ts &&
        runloom_tstate_attach_state(ts) == RUNLOOM_TS_DETACHED) {
        runloom_iframe_walk((void *)ts->current_frame, 1, hubinfo_top_cb, &ctx);
    }
    __atomic_store_n(&runloom_sysmon_hazard_ts, (void *)NULL, __ATOMIC_RELEASE);
    runloom_mutex_unlock(&runloom_sysmon_hazard_lock);
    *line_out = ctx.line;
    return ctx.code;
}

/* Format a frame as "qualname (base:line)" into buf.  Best-effort -- any
 * failure leaves "?" for the missing part. */
static void hubinfo_format_frame(PyCodeObject *code, int line,
                                 char *buf, size_t cap)
{
    PyObject *qn = NULL, *fn = NULL;
    const char *qs = NULL, *fs = NULL, *base, *b1, *b2;
    qn = PyObject_GetAttrString((PyObject *)code, "co_qualname");
    if (qn == NULL) { PyErr_Clear();
        qn = PyObject_GetAttrString((PyObject *)code, "co_name"); }
    fn = PyObject_GetAttrString((PyObject *)code, "co_filename");
    if (qn != NULL) qs = PyUnicode_AsUTF8(qn);
    if (fn != NULL) fs = PyUnicode_AsUTF8(fn);
    if (qs == NULL) { PyErr_Clear(); qs = "?"; }
    if (fs == NULL) { PyErr_Clear(); fs = "?"; }
    /* basename only -- the directory path is noise in a one-line view. */
    b1 = strrchr(fs, '/');
    b2 = strrchr(fs, '\\');
    base = (b1 > b2) ? b1 : b2;
    base = (base != NULL) ? base + 1 : fs;
    snprintf(buf, cap, "%s (%s:%d)", qs, base, line);
    Py_XDECREF(qn);
    Py_XDECREF(fn);
    if (PyErr_Occurred()) PyErr_Clear();
}

runloom_hub_info_t *runloom_mn_hub_snapshot(long *count_out)
{
    runloom_hub_info_t *arr;
    int n, i;
    long long now;

    if (count_out != NULL) *count_out = 0;
    n = runloom_hub_count;
    if (runloom_hubs == NULL || n <= 0) return NULL;

    arr = (runloom_hub_info_t *)calloc((size_t)n, sizeof(*arr));
    if (arr == NULL) return NULL;

    now        = runloom_monotonic_ns();

    for (i = 0; i < n; i++) {
        runloom_hub_t *h = &runloom_hubs[i];
        long long      start = __atomic_load_n(&h->resume_start_ns, __ATOMIC_RELAXED);
        runloom_g_t   *rg    = (runloom_g_t *)__atomic_load_n((void **)&h->resume_g, __ATOMIC_RELAXED);
        PyThreadState *hts   = (PyThreadState *)__atomic_load_n((void **)&h->tstate, __ATOMIC_ACQUIRE);
        int            tss   = runloom_sysmon_hub_attach_state(h, hts);
        runloom_hub_info_t *o = &arr[i];

        o->id                = h->id;
        o->pending           = __atomic_load_n(&h->pending, __ATOMIC_RELAXED);
        o->attach_state      = tss;
        o->blocked_at[0]     = '\0';
        o->has_running_g     = (start != 0) ? 1 : 0;
        o->dwell_ms          = (start != 0 && now > start)
                               ? (double)(now - start) / 1e6 : 0.0;
        o->running_g         = (start != 0 && rg != NULL) ? runloom_g_id(rg) : 0;

        /* blocked_at: only for a fiber wedged DETACHED past the wedge budget
         * (see the safety note above).  Off the hot path entirely -- this whole
         * block is skipped unless a hub has actually overrun the budget. */
        if (start != 0 && (now - start) > runloom_sysmon_wedge_ns &&
            tss == RUNLOOM_TS_DETACHED) {
            int line = 0;
            PyCodeObject *code = hubinfo_top_code(h, &line);
            if (code != NULL) {
                hubinfo_format_frame(code, line, o->blocked_at,
                                     sizeof o->blocked_at);
                Py_DECREF(code);
            }
        }
    }

    if (count_out != NULL) *count_out = n;
    return arr;
}
