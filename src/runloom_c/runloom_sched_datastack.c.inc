/* Part of runloom_sched.c -- #included into that translation unit, not compiled
 * standalone.  See runloom_sched.c for the includes/typedefs/file-scope state
 * these definitions depend on. */


void runloom_first_run_install_datastack(void)
{
    PyThreadState *ts = PyThreadState_GET();
    if (!runloom_chunk_pool_install(ts)) {
        ts->datastack_chunk = NULL;
        ts->datastack_top = NULL;
        ts->datastack_limit = NULL;
    }
}

/* ---- Datastack-tail idle reclaim (companion to the C-stack sweep) ----
 *
 * A Python fiber parked deep in CPython (e.g. in recv inside the eval
 * loop) holds a _PyStackChunk with its live frames in [chunk, datastack_top)
 * and unpushed free space in [datastack_top, datastack_limit).  The dwell
 * stack-sweep madvises the C stack below SP but NEVER this chunk, so the
 * chunk's free tail stays resident on every parked Python g.  This drops it.
 *
 * Only the CURRENT (top) chunk has a reclaimable tail: chunk->previous
 * chunks are full of live outer frames, so we touch only datastack_chunk.
 * The chunk is an arena allocation (mmap, page-aligned base, size a multiple
 * of the page), so datastack_limit is page-aligned and the geometry is clean.
 * We keep the partial page holding the live frontier and drop whole pages
 * strictly above it.  Refault on the next frame push is the (cheap, zero-
 * filled) cost, identical to the C-stack sweep's contract. */
#if defined(MADV_DONTNEED)
/* Decompose counters (RUNLOOM_DATASTACK_DEBUG only). */
static unsigned long long runloom_ds_sweep_tail_bytes = 0;
static unsigned long long runloom_ds_sweep_resident_bytes = 0;
static unsigned long long runloom_ds_sweep_chunks = 0;

/* mincore the [lo,hi) range and return resident bytes.  Best-effort: on any
 * error (e.g. mincore unsupported) returns 0 so accounting just under-counts
 * rather than misleads. */
static unsigned long long runloom_ds_resident_bytes(uintptr_t lo, uintptr_t hi,
                                                 size_t page)
{
    unsigned char vec[64];           /* covers up to 64 pages == 256 KiB tail */
    size_t npages = (size_t)((hi - lo) / page);
    unsigned long long resident = 0;
    size_t i;
    if (npages == 0 || npages > sizeof(vec)) return 0;
    if (mincore((void *)lo, (size_t)(hi - lo), vec) != 0) return 0;
    for (i = 0; i < npages; i++) {
        if (vec[i] & 1) resident += (unsigned long long)page;
    }
    return resident;
}
#endif

void runloom_sched_madvise_datastack_idle(runloom_g_t *g)
{
#if defined(MADV_DONTNEED)
    static int dbg = -1;             /* read RUNLOOM_DATASTACK_DEBUG once */
    runloom_pystate_snap_t *snap;
    uintptr_t top, limit, lo, hi;
    long ps;
    size_t page;

    /* Always on: the C-stack dwell sweep gates whether this runs at all (it's
     * called only from the sweep's batch loop) and supplies the churn
     * throttle, so on a flat handler -- where the tail is unfaulted and we
     * reclaim nothing -- this is just a cheap no-op madvise (measured: no
     * throughput cost); on the common deep-then-park shape it reclaims real
     * resident RAM. */
    if (g == NULL) return;
    snap = &g->snap;
    if (!snap->valid || snap->datastack_chunk == NULL ||
        snap->datastack_top == NULL || snap->datastack_limit == NULL) {
        return;                      /* C-only g, or no chunk installed */
    }

    ps = sysconf(_SC_PAGESIZE);
    page = (ps > 0) ? (size_t)ps : (size_t)4096;
    top   = (uintptr_t)snap->datastack_top;
    limit = (uintptr_t)snap->datastack_limit;
    if (limit <= top) return;
    lo = (top + page - 1) & ~(uintptr_t)(page - 1);   /* align UP past frontier */
    hi = limit & ~(uintptr_t)(page - 1);              /* align DOWN to chunk end */
    if (hi <= lo) return;            /* no whole free page to drop */

    {
        int d = __atomic_load_n(&dbg, __ATOMIC_RELAXED);
        if (d < 0) {
            const char *e = getenv("STACKWEAVE_DATASTACK_DEBUG");
            d = (e != NULL && e[0] != '0') ? 1 : 0;
            __atomic_store_n(&dbg, d, __ATOMIC_RELAXED);
        }
        if (d) {
            unsigned long long tail = (unsigned long long)(hi - lo);
            unsigned long long res = runloom_ds_resident_bytes(lo, hi, page);
            __atomic_add_fetch(&runloom_ds_sweep_tail_bytes, tail,
                               __ATOMIC_RELAXED);
            __atomic_add_fetch(&runloom_ds_sweep_resident_bytes, res,
                               __ATOMIC_RELAXED);
            __atomic_add_fetch(&runloom_ds_sweep_chunks, 1ULL, __ATOMIC_RELAXED);
        }
    }

    (void)madvise((void *)lo, (size_t)(hi - lo), MADV_DONTNEED);
#else
    (void)g;
#endif
}

void runloom_sched_datastack_sweep_stats(unsigned long long *tail_bytes,
                                      unsigned long long *resident_bytes,
                                      unsigned long long *chunks)
{
#if defined(MADV_DONTNEED)
    if (tail_bytes)
        *tail_bytes = __atomic_load_n(&runloom_ds_sweep_tail_bytes, __ATOMIC_RELAXED);
    if (resident_bytes)
        *resident_bytes = __atomic_load_n(&runloom_ds_sweep_resident_bytes,
                                          __ATOMIC_RELAXED);
    if (chunks)
        *chunks = __atomic_load_n(&runloom_ds_sweep_chunks, __ATOMIC_RELAXED);
#else
    if (tail_bytes) *tail_bytes = 0;
    if (resident_bytes) *resident_bytes = 0;
    if (chunks) *chunks = 0;
#endif
}

/* Drain the datastack-chunk chain currently attached to tstate,
 * returning tstate's datastack pointers to NULL.
 *
 * Called after a fiber completes, BEFORE we restore the scheduler
 * or hub snapshot (which would overwrite tstate->datastack_chunk with
 * the scheduler's saved value and leak g's chain).
 *
 * Reused chunks go to the per-thread pool (up to RUNLOOM_CHUNK_POOL_CAP).
 * Overflow goes back to the arena allocator that CPython's frame
 * allocator pulls from.
 *
 * Algorithm matches greenlet's PythonState::did_finish, plus pool reuse. */
void runloom_drain_g_datastack(void)
{
    PyThreadState *ts = PyThreadState_GET();
    _PyStackChunk *chunk = ts->datastack_chunk;
    PyObjectArenaAllocator alloc;

    if (chunk == NULL) return;

    ts->datastack_chunk = NULL;
    ts->datastack_top = NULL;
    ts->datastack_limit = NULL;

    PyObject_GetArenaAllocator(&alloc);
    while (chunk != NULL) {
        _PyStackChunk *prev = chunk->previous;
        runloom_chunk_pool_put(chunk, &alloc);
        chunk = prev;
    }
}

/* Install an initial root for the fiber's Python frame chain.  Run
 * inside runloom_g_entry, on the fiber's own stack, BEFORE we call any
 * user Python code.
 *
 * The point: when user code calls PyEval_EvalFrameDefault, the new
 * interpreter frame's `previous` field is linked to whatever was at
 * tstate's "top of chain" pointer.  If we don't sever the chain, that
 * "top" is whoever ran most recently on this OS thread -- the
 * scheduler, or worse, another fiber.  Then traceback walks and
 * recursion checks pull in every frame across every fiber.
 *
 * So we NULL out tstate->current_frame: g's first interpreter frame roots
 * its own chain, which walks back to a terminator, not to the previous
 * coroutine. */

static void runloom_install_initial_root_frame(void)
{
    PyThreadState *ts = PyThreadState_GET();
    ts->current_frame = NULL;
}

/* ---- Ready FIFO ops (ring buffer of g pointers) ----
 *
 * head/tail are MONOTONIC counters, masked into the ring on access.
 * As long as we don't wrap a size_t (which would take >5000 years at
 * 100M push/s on 64-bit), the (tail - head) arithmetic gives the
 * exact count.  Wraparound-safe for 32-bit too because we mask
 * differences, not absolute values.
 *
 * Grows on overflow by doubling; the ring is per-thread so no lock.
 */
static int runloom_ready_grow(runloom_sched_t *s)
{
    size_t old_cap = s->ready_cap;
    size_t new_cap = old_cap ? old_cap * 2 : 64;
    runloom_g_t **new_ring = (runloom_g_t **)PyMem_Calloc(new_cap, sizeof(runloom_g_t *));
    size_t i, head = s->ready_head, tail = s->ready_tail;
    if (new_ring == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    /* Copy from the old ring, preserving order.  Old has tail-head
     * entries starting at (head & old_mask). */
    for (i = 0; i < (tail - head); i++) {
        new_ring[i] = s->ready_ring[(head + i) & s->ready_mask];
    }
    PyMem_Free(s->ready_ring);
    s->ready_ring = new_ring;
    s->ready_cap = new_cap;
    s->ready_mask = new_cap - 1;
    /* relaxed-atomic: the cross-thread deadlock census (ready_empty) may read
     * these while grow runs on the owner -- see runloom_sched_ready_empty. */
    __atomic_store_n(&s->ready_head, (size_t)0, __ATOMIC_RELAXED);
    __atomic_store_n(&s->ready_tail, tail - head, __ATOMIC_RELAXED);
    return 0;
}

void runloom_sched_ready_push(runloom_sched_t *s, runloom_g_t *g)
{
    if (__builtin_expect(s->ready_tail - s->ready_head >= s->ready_cap, 0)) {
        if (runloom_ready_grow(s) < 0) {
            /* The ready ring is a critical per-thread scheduler structure and
             * this g's pending-count was already charged to its hub, so dropping
             * it here would orphan the fiber: h->pending never decrements, so
             * runloom_mn_run's pending-sum exit condition is never met and run()
             * hangs forever.  The old "OOM: drop g; caller checks PyErr" contract
             * never actually held -- NO caller of ready_push (not even the
             * single-thread spawn path, which returns the capsule unconditionally)
             * inspects PyErr or recovers, and several M:N hub_main callers run
             * with the GIL released and cannot.  Growth only fails on genuine
             * memory exhaustion, so fail loudly and immediately -- matching
             * runloom_sched_get's Py_FatalError on a failed sched allocation --
             * rather than degrading into a silent, undiagnosable permanent hang. */
            Py_FatalError("stackweave: out of memory growing the ready ring");
        }
    }
    s->ready_ring[s->ready_tail & s->ready_mask] = g;
    /* relaxed-atomic store: races the cross-thread deadlock census's read of
     * ready_tail (ready_empty); single-writer, so a plain owner read on the RHS
     * is fine.  Zero-cost on x86. */
    __atomic_store_n(&s->ready_tail, s->ready_tail + 1, __ATOMIC_RELAXED);
}

/* ====================================================================
 * PCT -- Probabilistic Concurrency Testing (controlled scheduler)
 *
 * Burckhardt, Kothari, Musuvathi, Nagarakatte, "A Randomized Scheduler with
 * Probabilistic Guarantees of Finding Bugs", ASPLOS 2010.  When RUNLOOM_PCT_SEED
 * is set, the single-hub ready-pop stops being FIFO and instead always runs
 * the highest-priority ready fiber; fibers get random priorities and
 * d-1 random "priority change points" demote a fiber mid-run.  This gives
 * a probabilistic lower bound of finding any depth-d bug.
 *
 * SCOPE: meaningful ONLY for the single-hub cooperative run() path, where the
 * ready-pop order IS the entire schedule.  It does NOT control the M:N hubs
 * (those are real parallel OS threads -- see the RUNLOOM_MN_SEED baton in
 * mn_sched_hub_resume_preempt.c.inc).  Zero cost when the env var is unset.
 *
 * Env: RUNLOOM_PCT_SEED (enable + seed), RUNLOOM_PCT_DEPTH (d, default 3),
 *      RUNLOOM_PCT_STEPS (estimated total scheduling steps k, default 2000),
 *      RUNLOOM_PCT_DEBUG (print the chosen change points once).
 * ==================================================================== */
#define PCT_MAX_DEPTH 16
typedef struct {
    int      inited;
    int      enabled;
    uint64_t rng;
    int      depth;
    long     steps;
    long     change_at[PCT_MAX_DEPTH];   /* d-1 ascending step indices */
    int      next_change;
    int      lo_ctr;                     /* assigns distinct low (demoted) prios */
} runloom_pct_t;
static runloom_pct_t runloom_pct;

static uint64_t runloom_pct_rand(void)      /* xorshift64 */
{
    uint64_t x = runloom_pct.rng;
    x ^= x << 13; x ^= x >> 7; x ^= x << 17;
    runloom_pct.rng = x;
    return x;
}

static void runloom_pct_init(void)
{
    const char *seed = getenv("STACKWEAVE_PCT_SEED");
    int i;
    long k;
    runloom_pct.inited = 1;
    if (seed == NULL || seed[0] == '\0') { runloom_pct.enabled = 0; return; }
    runloom_pct.enabled = 1;
    runloom_pct.rng = strtoull(seed, NULL, 10);
    if (runloom_pct.rng == 0) runloom_pct.rng = 0x9E3779B97F4A7C15ULL;  /* xorshift fixpoint */
    runloom_pct.depth = getenv("STACKWEAVE_PCT_DEPTH") ? atoi(getenv("STACKWEAVE_PCT_DEPTH")) : 3;
    if (runloom_pct.depth < 1) runloom_pct.depth = 1;
    if (runloom_pct.depth > PCT_MAX_DEPTH) runloom_pct.depth = PCT_MAX_DEPTH;
    k = getenv("STACKWEAVE_PCT_STEPS") ? atol(getenv("STACKWEAVE_PCT_STEPS")) : 2000;
    if (k < 1) k = 1;
    for (i = 0; i < runloom_pct.depth - 1; i++)
        runloom_pct.change_at[i] = 1 + (long)(runloom_pct_rand() % (uint64_t)k);
    for (i = 1; i < runloom_pct.depth - 1; i++) {   /* insertion sort ascending */
        long v = runloom_pct.change_at[i]; int j = i - 1;
        while (j >= 0 && runloom_pct.change_at[j] > v) {
            runloom_pct.change_at[j + 1] = runloom_pct.change_at[j]; j--;
        }
        runloom_pct.change_at[j + 1] = v;
    }
    runloom_pct.steps = 0;
    runloom_pct.next_change = 0;
    runloom_pct.lo_ctr = 0;
    if (getenv("STACKWEAVE_PCT_DEBUG")) {
        fprintf(stderr, "[pct] seed=%s depth=%d steps<=%ld change_at=",
                seed, runloom_pct.depth, k);
        for (i = 0; i < runloom_pct.depth - 1; i++)
            fprintf(stderr, "%ld%s", runloom_pct.change_at[i],
                    i + 1 < runloom_pct.depth - 1 ? "," : "\n");
        if (runloom_pct.depth <= 1) fprintf(stderr, "(none)\n");
    }
}

/* Find the logical ring index of the highest-priority ready g, assigning a
 * fresh random high priority to any g that doesn't have one yet.
 *
 * FIFO fibers (pct_fifo, the aio bridge's call_soon/task gs) must keep their
 * relative spawn order: only the OLDEST ready FIFO g is a candidate; younger
 * ones are skipped until it has run.  So PCT may still interleave reorderable
 * (raw) gs freely and may even defer the oldest FIFO g behind one, but it can
 * never run a later call_soon callback before an earlier one -- honouring
 * asyncio's call_soon-FIFO contract while still exploring raw concurrency. */
static size_t runloom_pct_argmax(runloom_sched_t *s)
{
    size_t p, best = s->ready_head;
    int best_prio = -2147483647 - 1;
    int seen_fifo = 0;
    for (p = s->ready_head; p != s->ready_tail; p++) {
        runloom_g_t *cg = s->ready_ring[p & s->ready_mask];
        if (cg->pct_fifo) {
            if (seen_fifo) continue;     /* keep FIFO gs in spawn order */
            seen_fifo = 1;
        }
        if (cg->pct_prio == 0)   /* unassigned -> random high priority */
            cg->pct_prio = 0x40000000 + (int)(runloom_pct_rand() & 0x3FFFFFFF);
        if (cg->pct_prio > best_prio) { best_prio = cg->pct_prio; best = p; }
    }
    return best;
}

static runloom_g_t *runloom_pct_pick(runloom_sched_t *s)
{
    size_t best, p;
    runloom_g_t *g;
    runloom_pct.steps++;
    best = runloom_pct_argmax(s);
    /* change point: demote the would-be winner below everyone, then re-pick,
     * so the schedule diverges at this step (one priority inversion). */
    if (runloom_pct.next_change < runloom_pct.depth - 1 &&
        runloom_pct.steps == runloom_pct.change_at[runloom_pct.next_change]) {
        s->ready_ring[best & s->ready_mask]->pct_prio = -(++runloom_pct.lo_ctr);
        runloom_pct.next_change++;
        best = runloom_pct_argmax(s);
    }
    g = s->ready_ring[best & s->ready_mask];
    /* remove logical slot `best` by shifting the rest of the queue down one */
    for (p = best; p + 1 != s->ready_tail; p++)
        s->ready_ring[p & s->ready_mask] = s->ready_ring[(p + 1) & s->ready_mask];
    s->ready_tail--;
    return g;
}

runloom_g_t *runloom_sched_ready_pop(runloom_sched_t *s)
{
    runloom_g_t *g;
    if (s->ready_head == s->ready_tail) return NULL;
    if (__builtin_expect(runloom_pct.enabled, 0))
        return runloom_pct_pick(s);
    g = s->ready_ring[s->ready_head & s->ready_mask];
    /* relaxed-atomic store: races the cross-thread deadlock census's read of
     * ready_head (ready_empty); single-writer.  Zero-cost on x86. */
    __atomic_store_n(&s->ready_head, s->ready_head + 1, __ATOMIC_RELAXED);
    /* Note: we deliberately do NOT clear g->next here -- the linked-
     * list `next` field is now repurposed by the g_slab as the free-
     * list link and by the M:N submission list.  Whichever consumer
     * uses it next overwrites the slot. */
    return g;
}

#define runloom_ready_push runloom_sched_ready_push
#define runloom_ready_pop  runloom_sched_ready_pop

/* STACKWEAVE_DEBUG=sleepheap: abort if a thread other than the owning hub's
 * mutates a hub's sleep heap.  The heap is a plain array with no lock, so a
 * second mutator corrupts it silently (a sleeper lost or scheduled twice) and
 * only TSan sees the race; this makes it a deterministic abort on a release
 * build.  Unowned heaps (single-thread scheds, a hub torn down by mn_fini after
 * its thread exited) are not checked.  Off by default: one predictable branch
 * per heap operation. */
static void runloom_sleep_owner_check(const runloom_sched_t *s, const char *op)
{
    void *owner = __atomic_load_n(&s->heap_owner, __ATOMIC_ACQUIRE);
    void *self = runloom_mn_current_hub_opaque();
    if (owner == NULL || owner == self) return;
    fprintf(stderr,
            "[STACKWEAVE_DEBUG=sleepheap] %s on hub %d's sleep heap from hub %d "
            "(-1: not a hub thread); only the owning hub's thread may mutate it\n",
            op, runloom_mn_hub_id_of(owner), runloom_mn_hub_id_of(self));
    fflush(stderr);
    abort();
}
#define RUNLOOM_SLEEP_OWNER_CHECK(s, op)                                     \
    do {                                                                     \
        if (RUNLOOM_DBG_ON(RUNLOOM_DBG_SLEEPHEAP))                           \
            runloom_sleep_owner_check((s), (op));                            \
    } while (0)

/* ---- Sleep heap (min-heap by (wake_at, seq)) ----
 * The entry's seq tiebreak makes equal-deadline sleepers wake in FIFO insertion
 * order, matching asyncio's (when, seq) TimerHandle ordering.  Without it,
 * two coros sleeping the same duration but started a tick apart could wake in
 * the wrong relative order (observed breaking equal-timeout races, e.g.
 * hypercorn's lifespan startup-timeout test). */
static inline int runloom_sleep_before(const runloom_sleep_entry_t *a,
                                       const runloom_sleep_entry_t *b)
{
    if (a->wake_at < b->wake_at) return 1;
    if (a->wake_at > b->wake_at) return 0;
    return a->seq < b->seq;
}

static int runloom_sleep_grow(runloom_sched_t *s)
{
    Py_ssize_t new_cap = s->sleep_cap ? s->sleep_cap * 2 : 16;
    runloom_sleep_entry_t *new_heap = (runloom_sleep_entry_t *)PyMem_Realloc(
        s->sleep_heap, sizeof(runloom_sleep_entry_t) * (size_t)(new_cap + 1));
    if (new_heap == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    s->sleep_heap = new_heap;
    s->sleep_cap = new_cap;
    return 0;
}

/* Push g's current sleep: g->wake_at, g->sleep_real, g->sleep_io and
 * g->sleep_ticket, which the caller has just set on this thread, are copied
 * into the entry.  A hub caller takes the entry's ref on g first, and drops it
 * if this fails. */
static int runloom_sleep_push(runloom_sched_t *s, runloom_g_t *g)
{
    Py_ssize_t i;
    runloom_sleep_entry_t e;
    RUNLOOM_SLEEP_OWNER_CHECK(s, "push");
    if (s->sleep_size + 1 > s->sleep_cap) {
        if (runloom_sleep_grow(s) < 0) return -1;
    }
    e.wake_at    = g->wake_at;
    e.seq        = s->sleep_seq_ctr++;   /* FIFO tiebreak, assigned at push time */
    e.g          = g;
    e.ticket     = __atomic_load_n(&g->sleep_ticket, __ATOMIC_RELAXED);
    e.sleep_real = g->sleep_real;
    e.sleep_io   = g->sleep_io;
    /* relaxed-atomic: the deadlock census (mn_has_wakeable_work) reads sleep_size
     * cross-thread; single-writer (the owning hub).  See runloom_sched_ready_empty. */
    __atomic_store_n(&s->sleep_size, s->sleep_size + 1, __ATOMIC_RELAXED);
    i = s->sleep_size;
    /* sift up: move e toward the root while it is earlier than its parent */
    while (i > 1 && runloom_sleep_before(&e, &s->sleep_heap[i / 2])) {
        s->sleep_heap[i] = s->sleep_heap[i / 2];
        i /= 2;
    }
    s->sleep_heap[i] = e;
    return 0;
}

/* Move the entry at index i down to its place (it may be later than a child). */
static void runloom_sleep_sift_down(runloom_sched_t *s, Py_ssize_t i)
{
    runloom_sleep_entry_t e = s->sleep_heap[i];
    Py_ssize_t child;
    for (;;) {
        child = i * 2;
        if (child > s->sleep_size) break;
        if (child + 1 <= s->sleep_size &&
            runloom_sleep_before(&s->sleep_heap[child + 1], &s->sleep_heap[child]))
            child++;
        /* stop once e is no later than the smaller child */
        if (!runloom_sleep_before(&s->sleep_heap[child], &e)) break;
        s->sleep_heap[i] = s->sleep_heap[child];
        i = child;
    }
    s->sleep_heap[i] = e;
}

/* Remove the entry at index i, from the MIDDLE of the heap: fill the hole with
 * the tail, then restore the invariant (up first, then down -- the tail can
 * belong on either side of the hole).  The other heap ops only ever touch the
 * root, so this is the one place that needs it.  Its only caller is the
 * single-thread signal delivery (runloom_sched_signal_wake_sleeper), which owns
 * the heap it edits.  A hub sleeper chosen for a signal is never removed: the
 * hub it parked on drops its entry (runloom_sched_sleep_purge). */
static void runloom_sleep_remove_at(runloom_sched_t *s, Py_ssize_t i)
{
    Py_ssize_t last_i = s->sleep_size;
    RUNLOOM_SLEEP_OWNER_CHECK(s, "remove");
    __atomic_store_n(&s->sleep_size, last_i - 1, __ATOMIC_RELAXED);
    if (i == last_i) return;                      /* removed the tail itself */
    s->sleep_heap[i] = s->sleep_heap[last_i];
    while (i > 1 && runloom_sleep_before(&s->sleep_heap[i], &s->sleep_heap[i / 2])) {
        runloom_sleep_entry_t t = s->sleep_heap[i];
        s->sleep_heap[i] = s->sleep_heap[i / 2];
        s->sleep_heap[i / 2] = t;
        i /= 2;
    }
    runloom_sleep_sift_down(s, i);
}

/* ====================================================================
 * Cross-thread signal delivery to an M:N sleep_io sleeper.
 *
 * runloom_sched_signal_wake_sleeper (below) walks the CALLER's sleep heap, which
 * is right for the single-thread plane and useless under M:N: a hub fiber
 * sleeping in select.poll's reprobe sits in ITS HUB's heap, and only the main
 * thread may run Python signal handlers.  Locking a hub's heap from here is not
 * an option -- the hub mutates it on every timer tick.
 *
 * So a hub sleeper REGISTERS a stack node in this process-wide list instead,
 * exactly as an io_uring parker does (see runloom_iou_sigwaiter_* in
 * io_uring_l_do.c.inc).  Nothing here touches a hub's heap: the main thread
 * only marks a node, claims the sleep and issues a wake.
 *
 * NOR DOES THE SLEEPER, once it resumes.  The wake goes through the global
 * run-queue, so the fiber can resume on ANY hub, and only the hub it parked on
 * may edit that hub's heap.  Its entry is abandoned instead: the claim makes
 * the entry's ticket stale, the main thread posts the origin hub's purge
 * mailbox, and that hub drops the entry on its own thread
 * (runloom_sched_sleep_purge; the pop would too, at the deadline).  It used to
 * remove itself, which raced the origin hub's timer pop and sleeps whenever it
 * had migrated -- TSan finding A1.
 *
 * NO BOUNDED HOLD.  The list walk is the whole decision: it either finds a
 * registered sleeper and the caller returns 1 (delivered), or it finds none and
 * the caller falls back to carrying the exception out of run() -- which is what
 * that path did unconditionally before.  The exception is never parked in a
 * mailbox waiting for a recipient to reappear, which is the property that made
 * dd8d0008's design unsafe.
 *
 * The lock covers link/unlink/walk.  A link costs one uncontended acquire per
 * cooperative-blocking reprobe (50 ms apart by default -- _SELECTOR_REPROBE_S),
 * and the walk runs only once a handler has actually raised. */
typedef struct runloom_sleep_sigwaiter {
    runloom_g_t                     *g;
    void                            *hub;    /* hub the sleeper parked on */
    runloom_sched_t                 *sched;  /* that hub's sched: owns signal_exc
                                              * and the sleeper's heap entry */
    uint64_t                         ticket; /* g->sleep_ticket of this sleep */
    int                              signaled;
    struct runloom_sleep_sigwaiter  *next;
    struct runloom_sleep_sigwaiter **pprev;
} runloom_sleep_sigwaiter_t;

static runloom_sleep_sigwaiter_t *runloom_sleep_sigwaiters;
static runloom_mutex_t runloom_sleep_sigwaiter_lock;
static volatile int runloom_sleep_sigwaiter_ready;   /* 0 none, 2 initialising, 1 ready */

/* Lazy one-shot init -- the CAS-then-spin shape used by rl_cancel_lock and the
 * io_uring sigwaiter lock; a plain `if (!inited)` would let two hub threads
 * both mutex_init the same lock. */
static void runloom_sleep_sigwaiter_lock_init(void)
{
    int expected = 0;
    if (__atomic_load_n(&runloom_sleep_sigwaiter_ready, __ATOMIC_ACQUIRE) == 1) return;
    if (__atomic_compare_exchange_n(&runloom_sleep_sigwaiter_ready, &expected, 2, 0,
                                    __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        runloom_mutex_init(&runloom_sleep_sigwaiter_lock);
        __atomic_store_n(&runloom_sleep_sigwaiter_ready, 1, __ATOMIC_RELEASE);
        return;
    }
    while (__atomic_load_n(&runloom_sleep_sigwaiter_ready, __ATOMIC_ACQUIRE) != 1) { }
}

static void runloom_sleep_sigwaiter_link(runloom_sleep_sigwaiter_t *w,
                                         runloom_g_t *g, void *hub,
                                         runloom_sched_t *sched, uint64_t ticket)
{
    runloom_sleep_sigwaiter_lock_init();
    RUNLOOM_RLOCK(&runloom_sleep_sigwaiter_lock, RUNLOOM_RANK_SLEEP_SIGWAITER);
    w->g        = g;
    w->hub      = hub;
    w->sched    = sched;
    w->ticket   = ticket;
    w->signaled = 0;
    w->next     = runloom_sleep_sigwaiters;
    w->pprev    = &runloom_sleep_sigwaiters;
    if (w->next != NULL) w->next->pprev = &w->next;
    runloom_sleep_sigwaiters = w;
    RUNLOOM_RUNLOCK(&runloom_sleep_sigwaiter_lock, RUNLOOM_RANK_SLEEP_SIGWAITER);
}

static void runloom_sleep_sigwaiter_unlink(runloom_sleep_sigwaiter_t *w)
{
    runloom_sleep_sigwaiter_lock_init();
    RUNLOOM_RLOCK(&runloom_sleep_sigwaiter_lock, RUNLOOM_RANK_SLEEP_SIGWAITER);
    if (w->pprev != NULL) {
        *w->pprev = w->next;
        if (w->next != NULL) w->next->pprev = w->pprev;
        w->pprev = NULL;
        w->next  = NULL;
    }
    RUNLOOM_RUNLOCK(&runloom_sleep_sigwaiter_lock, RUNLOOM_RANK_SLEEP_SIGWAITER);
}

/* Gate predicate: is any hub fiber parked in a cooperative reprobe sleep?
 * Keeps NEVER FETCH A SIGNAL YOU CANNOT DELIVER true for this recipient class --
 * the caller must not run handlers unless some deliverable exists. */
int runloom_sched_has_mn_sleep_io_waiter(void)
{
    int found = 0;
    runloom_sleep_sigwaiter_t *w;
    runloom_sleep_sigwaiter_lock_init();
    RUNLOOM_RLOCK(&runloom_sleep_sigwaiter_lock, RUNLOOM_RANK_SLEEP_SIGWAITER);
    for (w = runloom_sleep_sigwaiters; w != NULL; w = w->next)
        if (w->g != NULL && !w->signaled) { found = 1; break; }
    RUNLOOM_RUNLOCK(&runloom_sleep_sigwaiter_lock, RUNLOOM_RANK_SLEEP_SIGWAITER);
    return found;
}

/* Hand `exc` to one registered hub sleeper.  Returns 1 if a recipient was
 * marked (the fiber WILL raise it at its resume point), 0 if there is none and
 * the caller must fall back to carrying it out of run().
 *
 * Marking happens under the lock, which is what makes it safe against a
 * sleeper that is concurrently waking: the sleeper unlinks its node under this
 * same lock BEFORE testing `signaled`, so a node we can still see belongs to a
 * fiber that has not yet finished its post-yield check, and it will observe the
 * mark.  Reserving the target's signal_exc slot comes FIRST, the same order the
 * netpoll and io_uring arms use: marking first and then finding the slot busy
 * would leave a fiber flagged with nobody delivering to it. */
int runloom_sched_sleep_signal_wake(PyObject *exc)
{
    runloom_sleep_sigwaiter_t *w;
    runloom_sleep_sigwaiter_lock_init();
    RUNLOOM_RLOCK(&runloom_sleep_sigwaiter_lock, RUNLOOM_RANK_SLEEP_SIGWAITER);
    for (w = runloom_sleep_sigwaiters; w != NULL; w = w->next) {
        runloom_sched_t *tgt = w->sched;
        PyObject *slot = NULL;
        runloom_g_t *g;
        void *hub;
        int claimed;
        if (w->g == NULL || w->signaled || tgt == NULL) continue;
        /* RELEASE: read by the hub thread in the sleeper's pickup. */
        if (!__atomic_compare_exchange_n(&tgt->signal_exc, &slot, exc, 0,
                                         __ATOMIC_RELEASE, __ATOMIC_RELAXED))
            continue;                       /* that hub already holds one */
        w->signaled = 1;
        /* NOTE: no signal_target write.  That field is the single-thread
         * plane's gate, and setting it here would be a plain cross-thread
         * store racing the hub's own read/clear.  It is also redundant: the
         * signal_exc CAS above admits exactly ONE recipient per sched, and
         * `signaled` on this node -- set under the lock the sleeper unlinks
         * under -- names which fiber that is.  The io_uring arm
         * (runloom_iou_deliver_signal) gates the same way. */
        g   = w->g;
        hub = w->hub;
        /* Claim this sleep (see g->sleep_ticket).  Losing means the hub's timer
         * pop already took the sleeper off the heap and will schedule it -- we
         * must not enqueue it a second time, and we don't need to: it is
         * already on its way to the post-yield check that consumes the mark
         * above. */
        {   uint64_t expected = w->ticket;
            claimed = __atomic_compare_exchange_n(&g->sleep_ticket, &expected,
                                                  w->ticket | 1, 0,
                                                  __ATOMIC_ACQ_REL,
                                                  __ATOMIC_ACQUIRE);
        }
        RUNLOOM_RUNLOCK(&runloom_sleep_sigwaiter_lock, RUNLOOM_RANK_SLEEP_SIGWAITER);
        /* Outside the lock: the wake re-enters scheduler code. */
        if (claimed) {
            /* Winning abandoned the sleeper's entry on tgt's heap, which only
             * tgt's hub may edit: post its purge mailbox (RELEASE, after the
             * claim, so the purge sees the claimed ticket) and kick it so it
             * drops the entry now rather than at the deadline -- an abandoned
             * entry keeps sleep_size non-zero and holds its g until then. */
            __atomic_store_n(&tgt->sleep_purge, 1, __ATOMIC_RELEASE);
            /* Dekker with tgt's idle announce: the hub stores idle_waiting,
             * fences, then re-checks sleep_purge before its condvar wait; we
             * store sleep_purge, fence, then read idle_waiting in the kick
             * below.  So either the hub sees the mailbox and skips the wait,
             * or the kick sees the hub waiting and signals it.  Explicit here
             * rather than borrowed from a fence inside the wake path. */
            __atomic_thread_fence(__ATOMIC_SEQ_CST);
            if (hub != NULL) runloom_mn_wake_g(hub, g);
            else             runloom_sched_wake_safe(g);
            runloom_mn_hub_kick(hub);
        }
        return 1;
    }
    RUNLOOM_RUNLOCK(&runloom_sleep_sigwaiter_lock, RUNLOOM_RANK_SLEEP_SIGWAITER);
    return 0;
}

/* Is any fiber of `s` sleeping inside a cooperative blocking call? */
int runloom_sched_has_sleep_io_waiter(runloom_sched_t *s)
{
    Py_ssize_t i;
    if (s == NULL) return 0;
    for (i = 1; i <= s->sleep_size; i++) {
        runloom_g_t *g = s->sleep_heap[i].g;
        if (g != NULL && g->sleep_io && g->owner == s) return 1;
    }
    return 0;
}

/* Hand the stashed signal to one such fiber: name it in s->signal_target, take
 * it out of the sleep heap and make it runnable NOW.  It picks the exception up
 * at its resume point in runloom_sched_sleep_until_ex, in its OWN stack -- the
 * sleeper analogue of wait_fd's RUNLOOM_NETPOLL_SIGNALED sentinel.
 *
 * Waking it immediately rather than letting the deadline arrive is what keeps
 * the "never fetch a signal you cannot deliver" invariant true: the exception
 * is never held across an unbounded window, so it needs no expiry bound and
 * cannot be stranded (the two failure modes of the reverted dd8d0008). */
int runloom_sched_signal_wake_sleeper(runloom_sched_t *s)
{
    Py_ssize_t i;
    if (s == NULL) return 0;
    for (i = 1; i <= s->sleep_size; i++) {
        runloom_g_t *g = s->sleep_heap[i].g;
        if (g == NULL || !g->sleep_io || g->owner != s) continue;
        runloom_sleep_remove_at(s, i);
        s->signal_target = g;
        runloom_ready_push(s, g);
        return 1;
    }
    return 0;
}

const runloom_sleep_entry_t *runloom_sched_sleep_peek(runloom_sched_t *s)
{
    if (s->sleep_size == 0) return NULL;
    return &s->sleep_heap[1];
}

/* Pop the earliest entry into *out; returns 1 if popped, 0 if the heap is
 * empty.  On a hub heap the caller now owns the entry's ref on out->g. */
int runloom_sched_sleep_pop_entry(runloom_sched_t *s, runloom_sleep_entry_t *out)
{
    RUNLOOM_SLEEP_OWNER_CHECK(s, "pop");
    if (s->sleep_size == 0) return 0;
    *out = s->sleep_heap[1];
    s->sleep_heap[1] = s->sleep_heap[s->sleep_size];
    __atomic_store_n(&s->sleep_size, s->sleep_size - 1, __ATOMIC_RELAXED);  /* census reads x-thread */
    if (s->sleep_size > 0) runloom_sleep_sift_down(s, 1);
    return 1;
}

/* The single-thread plane's pop: just the g (its entries hold no ref). */
runloom_g_t *runloom_sched_sleep_pop(runloom_sched_t *s)
{
    runloom_sleep_entry_t e;
    return runloom_sched_sleep_pop_entry(s, &e) ? e.g : NULL;
}

/* Drop every ABANDONED entry from a hub's heap, on that hub's thread (its loop
 * top, once the purge mailbox s->sleep_purge is set).  An entry is abandoned
 * once its g's sleep_ticket no longer matches the entry's copy: a signal wake
 * claimed that sleep, so the timer must not fire it, and the fiber may since
 * have resumed on another hub and be sleeping again, here or there.  Only an
 * io sleep has a signal waker, so every other entry is kept without touching
 * its g: a heap of plain sleepers costs one flag test per entry, not a cache
 * miss on each g.
 *
 * Partition the live entries to the front, re-heapify (Floyd), then release
 * the abandoned entries' refs.  The decrefs come last, with the heap already
 * consistent: the last ref of a finished fiber tears it down right here, which
 * can run Python finalizers (none can push onto this heap -- no fiber is
 * current on a hub at its loop top).  O(n), the cost of the linear find it
 * replaces, once per signal delivered to a hub sleeper.  An entry abandoned
 * after its check is left for the next purge, or for the pop. */
void runloom_sched_sleep_purge(runloom_sched_t *s)
{
    Py_ssize_t i, n = 0, size = s->sleep_size;
    RUNLOOM_SLEEP_OWNER_CHECK(s, "purge");
    for (i = 1; i <= size; i++) {
        runloom_sleep_entry_t e = s->sleep_heap[i];
        if (e.sleep_io &&
            __atomic_load_n(&e.g->sleep_ticket, __ATOMIC_ACQUIRE) != e.ticket)
            continue;                              /* abandoned: stays behind */
        n++;                                       /* swap into the live prefix: */
        s->sleep_heap[i] = s->sleep_heap[n];       /* slot n is abandoned, or i */
        s->sleep_heap[n] = e;
    }
    if (n == size) return;
    __atomic_store_n(&s->sleep_size, n, __ATOMIC_RELAXED);  /* census reads x-thread */
    for (i = n / 2; i >= 1; i--) runloom_sleep_sift_down(s, i);
    for (i = n + 1; i <= size; i++) runloom_g_decref(s->sleep_heap[i].g);
}

#define runloom_sleep_peek runloom_sched_sleep_peek
#define runloom_sleep_pop  runloom_sched_sleep_pop

/* ---- Timer heap (min-heap by deadline) -- in-memory TIMED parks ----
 * By-VALUE entries { deadline, g } (no g->wake_at reuse, so a g may hold several
 * stale entries across re-parks).  Drained alongside the sleep heap; the drain
 * fires a due entry by CASing the g's parked_safe (the same exactly-once arbiter
 * as wake_safe).  See runloom_park_generic_timed. */
static int runloom_timer_grow(runloom_sched_t *s)
{
    Py_ssize_t new_cap = s->timer_cap ? s->timer_cap * 2 : 16;
    runloom_timer_entry_t *nh = (runloom_timer_entry_t *)PyMem_Realloc(
        s->timer_heap, sizeof(runloom_timer_entry_t) * (size_t)(new_cap + 1));
    if (nh == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    s->timer_heap = nh;
    s->timer_cap = new_cap;
    return 0;
}

static int runloom_timer_push(runloom_sched_t *s, double deadline, runloom_g_t *g,
                              int logical)
{
    Py_ssize_t i;
    runloom_timer_entry_t e;
    if (s->timer_size + 1 > s->timer_cap) {
        if (runloom_timer_grow(s) < 0) return -1;
    }
    /* The timer entry holds a REFERENCE to g.  Without it, a g woken early (by a
     * real wake, not the timeout) can complete and be FREED (coros beyond the
     * pool cap are munmap'd) while its stale entry lingers on the heap until its
     * deadline; runloom_sched_drain_timers then CASes e.g->parked_safe on freed
     * memory -> use-after-free SEGV (flaky, fired by p49_barrier_stress @ 1M with
     * its timeout=10 barrier waits; the -fanalyzer "uninitialized e.g" warning
     * pointed here).  Balanced by a decref at every pop in drain_timers and a
     * runloom_sched_release_timers() sweep at teardown. */
    runloom_g_incref(g);
    e.deadline = deadline;
    e.g = g;
    e.logical = logical;
    __atomic_store_n(&s->timer_size, s->timer_size + 1, __ATOMIC_RELAXED);  /* census reads x-thread */
    i = s->timer_size;
    while (i > 1 && s->timer_heap[i / 2].deadline > deadline) {  /* sift up */
        s->timer_heap[i] = s->timer_heap[i / 2];
        i /= 2;
    }
    s->timer_heap[i] = e;
    return 0;
}

/* Peek the earliest deadline (valid until the next push/pop), or NULL if empty. */
static runloom_timer_entry_t *runloom_timer_peek(runloom_sched_t *s)
{
    if (s->timer_size == 0) return NULL;
    return &s->timer_heap[1];
}

/* Pop the earliest entry into *out; returns 1 if popped, 0 if the heap is empty. */
static int runloom_timer_pop(runloom_sched_t *s, runloom_timer_entry_t *out)
{
    runloom_timer_entry_t last;
    Py_ssize_t i, child;
    if (s->timer_size == 0) return 0;
    *out = s->timer_heap[1];
    last = s->timer_heap[s->timer_size];
    __atomic_store_n(&s->timer_size, s->timer_size - 1, __ATOMIC_RELAXED);  /* census reads x-thread */
    if (s->timer_size == 0) return 1;
    i = 1;
    while (1) {                                                 /* sift down */
        child = i * 2;
        if (child > s->timer_size) break;
        if (child + 1 <= s->timer_size &&
            s->timer_heap[child + 1].deadline < s->timer_heap[child].deadline) {
            child++;
        }
        if (s->timer_heap[child].deadline >= last.deadline) break;
        s->timer_heap[i] = s->timer_heap[child];
        i = child;
    }
    s->timer_heap[i] = last;
    return 1;
}

/* Earliest timer deadline (monotonic seconds) on s, or -1.0 if no timers.
 * Used by the drain / hub idle-wait to bound the block so a due timer fires. */
double runloom_sched_timer_next_deadline(runloom_sched_t *s)
{
    if (s->timer_size == 0) return -1.0;
    return s->timer_heap[1].deadline;
}

/* ---- Scheduler lifecycle ---- */
/* Phase C: per-thread schedulers (see runloom_sched_get below).  runloom_cal_default
 * holds the calibrated default stack size that each new thread's sched is
 * initialised with. */

/* ---- Stack calibration ----
 *
 * Default: 512 KB.  This covers everything a fiber realistically does --
 * the deepest stdlib single frame (`_decimal` squaretrans_pow2, 256 KB), full
 * TLS/SSH crypto in a callback (the aio bridge runs asyncssh's kex + OpenSSL on
 * 512 KB), nested parsers -- so ordinary code never hits the guard page.  It's
 * cheap: stacks are demand-paged (the unused tail consumes no physical RAM until
 * touched -- VM, not RSS, verified on Linux/macOS mmap), so the cost is virtual
 * address space, not memory.  Calibration adapts UP for stack-hungry programs (freeze to next_pow2(max_hwm * RUNLOOM_CAL_SAFETY)
 * after RUNLOOM_CAL_TARGET completions) but is FLOORED at this default -- it never
 * auto-shrinks below the safe size and re-exposes the deep cases.  Reclaiming the
 * tail for huge fiber counts is the job of the M:N grow-down (default-on,
 * stackweave/runtime.py) and the opt-in per-kind auto-sizer
 * (stackweave.inspect.enable_stack_autosize), not calibration's; neither goes
 * below RUNLOOM_FT314_MIN_STACK_SIZE (256 KB, runloom_sched.h), the floor on
 * every fiber stack.  A true outlier deeper than 512 KB is a clean guard-page
 * crash + a one-line pin (stackweave.fiber(fn, stack_size=...)), never
 * corruption.
 *
 * Reads/writes are plain (single-threaded scheduler in v0); when
 * Phase C arrives these will move under the scheduler lock. */
#define RUNLOOM_DEFAULT_STACK_SIZE   (512 * 1024)
/* RUNLOOM_MIN_STACK_SIZE / RUNLOOM_MAX_STACK_SIZE now live in runloom_sched.h
 * (shared with mn_sched.c so the M:N spawn path clamps to the same bounds). */
#define RUNLOOM_CAL_TARGET           1000     /* gs before freeze */
#define RUNLOOM_CAL_SAFETY           4        /* multiply HWM by this */

static size_t    runloom_cal_default = RUNLOOM_DEFAULT_STACK_SIZE;
static size_t    runloom_cal_max_hwm = 0;
static long long runloom_cal_completed = 0;
