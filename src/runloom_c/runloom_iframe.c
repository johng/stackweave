/* runloom_iframe.c -- the ONLY translation unit that reaches into CPython's
 * internal interpreter-frame layout.  Kept separate so Py_BUILD_CORE_MODULE
 * (and the internal headers it unlocks) never leak into the rest of the
 * build.  See runloom_iframe.h. */

/* internal/pycore_frame.h requires the core-build macro. */
#ifndef Py_BUILD_CORE_MODULE
#  define Py_BUILD_CORE_MODULE 1
#endif

#define PY_SSIZE_T_CLEAN
#include <Python.h>

/* Same requirement as runloom_sched.h (this TU does not include it). */
#if PY_VERSION_HEX < 0x030E0000 || !defined(Py_GIL_DISABLED)
#  error "stackweave requires a free-threaded (--disable-gil) CPython 3.14 or newer"
#endif

#include "runloom_iframe.h"
#include "coro.h"    /* runloom_coro_stack_base/size (fiber C-stack geometry) */

#if !defined(RUNLOOM_NO_IFRAME)
#  include "internal/pycore_frame.h"
/* 3.14 moved the complete _PyInterpreterFrame struct + FRAME_OWNED_BY_CSTACK out
 * of pycore_frame.h (now only a forward declaration) into pycore_interpframe.h. */
#  include "internal/pycore_interpframe.h"
#  define RUNLOOM_IFRAME_HAVE 1
#endif

#if !defined(RUNLOOM_NO_IFRAME)
#  include "internal/pycore_pystate.h"     /* _PyThreadStateImpl */
#  include "internal/pycore_brc.h"        /* struct _brc_thread_state */
#  include "internal/pycore_ceval.h"      /* _Py_HandlePending, _PY_EVAL_EXPLICIT_MERGE_BIT */
#  include "internal/pycore_interp.h"     /* interp->brc.table (bucket by thread id) */
#  include "internal/pycore_llist.h"      /* bucket_node reordering */
#  include "internal/pycore_critical_section.h"  /* _PyCriticalSection_* */
#  include "internal/pycore_tstate.h"   /* _PyThreadState_SetAllocHome (Py_TSTATE_ALLOC_HOME) */
#  define RUNLOOM_CRITSEC_HAVE 1
#  define RUNLOOM_DESTRUCT_HAVE 1
#endif

/* GC visibility for parked-fiber frames: needs the free-threaded 3.14 collector
 * bit + stackref predicates + the stop-the-world state on the interpreter.  Only
 * available where the internal frame layout is (RUNLOOM_IFRAME_HAVE) and the
 * free-threaded GC exists (Py_GIL_DISABLED, 3.14+). */
#if defined(RUNLOOM_IFRAME_HAVE)
#  include "internal/pycore_gc.h"          /* _PyGC_BITS_UNREACHABLE; ob_gc_bits */
#  include "internal/pycore_stackref.h"    /* PyStackRef_IsNullOrInt / IsDeferred / Borrow */
#  include "internal/pycore_interp.h"      /* PyInterpreterState.stoptheworld.world_stopped */
#  define RUNLOOM_GCFRAMES_HAVE 1
#endif

/* See runloom_iframe.h.  Freezes op's refcount (immortal) so cross-hub
 * incref/decref become no-ops -- the A1b hub-scaling experiment lever.  We set
 * the immortal refcount fields directly (mirroring _Py_SetImmortalUntracked):
 * the real _Py_SetImmortal is internal-link-only (not exported from the shared
 * libpython), but _Py_IMMORTAL_REFCNT_LOCAL / _Py_UNOWNED_TID are public macros
 * in object.h.  Leaves the object GC-tracked (immortal objects are skipped by
 * the collector anyway), which is exactly what _Py_SetImmortalUntracked does. */
void runloom_immortalize(PyObject *op)
{
    if (op == NULL) {
        return;
    }
    op->ob_tid       = _Py_UNOWNED_TID;
    op->ob_ref_local = _Py_IMMORTAL_REFCNT_LOCAL;
    op->ob_ref_shared = 0;
}

/* Borrow `home`'s allocator (mimalloc heap + qsbr/page-reclaim) for `exec` --
 * the per-g cross-hub migration fix.  With the optional CPython patch
 * (Py_TSTATE_ALLOC_HOME, see patches/) a per-g tstate carries no live heap and
 * allocates on whichever hub is running it, so no per-fiber heap ever migrates
 * OS threads -- removing the _mi_page_retire teardown corruption.  Compiled as
 * a no-op when built against stock CPython. */
void runloom_iframe_borrow_alloc_home(PyThreadState *exec, PyThreadState *home)
{
#if defined(Py_TSTATE_ALLOC_HOME)
    _PyThreadState_SetAllocHome(exec, home);
#else
    (void)exec; (void)home;
#endif
}

/* See runloom_iframe.h.  Free-threaded CPython keeps the object freelists
 * (ints, floats, tuples, lists, dicts, ...) per thread state, and
 * PyThreadState_Clear drains the CURRENT thread state's freelists
 * (_Py_freelists_GET()), not those of the state being cleared -- CPython's
 * own threads always clear themselves.  A per-g tstate is cleared by whichever
 * thread drops the last g ref, with that thread's state current, so whatever
 * the fiber had cached in its freelists was never freed: every block stayed
 * "used" in the hub heap it came from, and once that hub's heap was abandoned
 * at mn_fini its segments could never be reclaimed.  The interpreter's
 * abandoned-segment pool then only grew, and every later PyThreadState_Clear
 * (one per fiber) re-walks that pool -- the per-fiber cost behind the
 * process-wide spawn->run slowdown after a fiber-heavy load.
 *
 * Move the dead state's entries onto the matching lists of the current state
 * instead; the Clear that follows then frees them with the right per-type
 * deallocator.  Generic over `struct _Py_freelists` (an array of
 * `struct _Py_freelist`, linked through each block's first word), so it needs
 * no table of deallocators.  Should CPython ever drain the cleared state's own
 * lists instead, the spliced entries simply stay cached on the current state
 * (reused, or freed when it clears) -- never leaked either way. */
void runloom_iframe_hand_over_freelists(PyThreadState *dead)
{
#if defined(RUNLOOM_DESTRUCT_HAVE)
    PyThreadState *cur = PyThreadState_GetUnchecked();
    struct _Py_freelist *src, *dst;
    size_t i, n;
    _Static_assert(sizeof(struct _Py_freelists) % sizeof(struct _Py_freelist) == 0,
                   "struct _Py_freelists is no longer a plain array of _Py_freelist");
    if (dead == NULL || cur == NULL || cur == dead) return;
    src = (struct _Py_freelist *)&((_PyThreadStateImpl *)dead)->freelists;
    dst = (struct _Py_freelist *)&((_PyThreadStateImpl *)cur)->freelists;
    n = sizeof(struct _Py_freelists) / sizeof(struct _Py_freelist);
    for (i = 0; i < n; i++) {
        void *head = src[i].freelist, *tail = head;
        Py_ssize_t k = 1;
        if (head == NULL) continue;
        while (*(void **)tail != NULL) {
            tail = *(void **)tail;
            k++;
        }
        *(void **)tail = dst[i].freelist;
        dst[i].freelist = head;
        /* size -1 marks a list disabled by an earlier Clear on this thread;
         * entries on it are still drained by the next clear. */
        dst[i].size = (dst[i].size > 0 ? dst[i].size : 0) + k;
        src[i].freelist = NULL;
        src[i].size = 0;
    }
#else
    (void)dead;
#endif
}

/* See runloom_iframe.h.  PyThreadState_Clear ends by abandoning the cleared
 * state's four mimalloc heaps, and each abandon also sweeps the interpreter's
 * abandoned-segment pool on behalf of other threads (_mi_abandoned_collect:
 * pop a segment, walk all its pages, free it if empty, else park it on the
 * visited list -- up to 1024 times per heap, cycling the visited list back
 * in, so a single segment that still holds live blocks costs the full 1024
 * walks).  CPython pays that once per OS-thread exit.  A per-g tstate pays it
 * once per FIBER, and the pool is never empty for long under M:N: every
 * mn_fini abandons the hub heaps, and anything still alive that a fiber
 * allocated keeps its segment there.  A quiet hub then spends 70-95 us tearing
 * down each trivial fiber instead of ~5 us, for the rest of the process.
 *
 * A borrower tstate (alloc-home: it allocates on the running hub's heap) owns
 * no segment and no page, so abandoning its heaps has nothing of its own to
 * do -- only the pool sweep, which the threads that do allocate still perform
 * whenever they need memory (mimalloc's reclaim-on-allocate) and on their
 * exit.  So when the state provably owns nothing, point its segment tld at an
 * empty private pool for the duration of the Clear.  Checked against the
 * mimalloc 2.1.2 that CPython 3.14 and 3.15 vendor (3.15.0rc1/rc2's
 * Objects/mimalloc/segment.c, mi_abandoned_pool_t and pycore_mimalloc.h are
 * byte-identical to 3.14.4's); other versions keep the plain Clear until
 * checked. */
void runloom_iframe_clear_fiber_tstate(PyThreadState *ts)
{
    runloom_iframe_hand_over_freelists(ts);
#if defined(RUNLOOM_DESTRUCT_HAVE) && PY_VERSION_HEX < 0x03100000
    {
        struct _mimalloc_thread_state *m = &((_PyThreadStateImpl *)ts)->mimalloc;
        int i, owns_nothing = m->tld.segments.count == 0;
        for (i = 0; i < _Py_MIMALLOC_HEAP_COUNT && owns_nothing; i++) {
            owns_nothing = m->heaps[i].page_count == 0
                && __atomic_load_n((void **)&m->heaps[i].thread_delayed_free,
                                   __ATOMIC_ACQUIRE) == NULL;
        }
        if (owns_nothing) {
            mi_abandoned_pool_t empty;
            mi_abandoned_pool_t *shared = m->tld.segments.abandoned;
            memset(&empty, 0, sizeof(empty));
            m->tld.segments.abandoned = &empty;
            PyThreadState_Clear(ts);
            m->tld.segments.abandoned = shared;
            return;
        }
    }
#endif
    PyThreadState_Clear(ts);
}

int runloom_iframe_service_merge_queue(PyThreadState *ts)
{
    int rounds = 0;
    if (ts == NULL) return 0;
    /* One relaxed load on the fast path: the bit is clear almost always. */
    while (_Py_eval_breaker_bit_is_set(ts, _PY_EVAL_EXPLICIT_MERGE_BIT)) {
        /* _Py_HandlePending is the eval loop's own dispatcher for this bit
         * (the merge routine itself is not exported).  It also services any
         * other pending bit on this state -- a scheduled GC, a stop-the-world
         * request -- exactly as the eval loop would at a safe point, which is
         * what a hub sitting attached between resumes is. */
        if (_Py_HandlePending(ts) < 0) {
            PyErr_Clear();          /* a deallocator's error; not ours to raise */
            break;
        }
        if (++rounds >= 64) break;  /* pathological re-queue chain; next resume continues */
    }
    return rounds;
}

static inline struct _brc_bucket *runloom_brc_bucket(PyInterpreterState *interp, uintptr_t tid)
{
    return &interp->brc.table[tid % _Py_BRC_NUM_BUCKETS];
}

/* Move `node` to the FRONT of `bucket`'s list (llist_insert_tail inserts
 * before its first argument, so "before the current first" is the front). */
static inline void runloom_brc_move_to_front(struct _brc_bucket *bucket, struct llist_node *node)
{
    llist_remove(node);
    llist_insert_tail(bucket->root.next, node);
}

void runloom_iframe_brc_adopt(PyThreadState *fiber, PyThreadState *hub)
{
    _PyThreadStateImpl *f = (_PyThreadStateImpl *)fiber;
    _PyThreadStateImpl *h = (_PyThreadStateImpl *)hub;
    uintptr_t tid = h->brc.tid;
    struct _brc_bucket *nb = runloom_brc_bucket(fiber->interp, tid);
    /* Identity for introspection: tstate->thread_id is what
     * sys._current_frames() / _current_exceptions() and faulthandler key on,
     * and it was the SPAWNER's thread for the fiber's whole life, so the
     * running fiber never appeared under the thread id it reports through
     * threading.get_ident().  While it runs it carries the hub's id; while
     * parked (release below) it carries a value that is no thread's, so it
     * cannot shadow the running fiber under the hub's id.  _current_frames
     * skips states with no frame and lets the OLDEST duplicate win, which
     * with this scheme is the hub's frameless state -> skipped -> the fiber. */
    fiber->thread_id = hub->thread_id;
    fiber->native_thread_id = hub->native_thread_id;
    if (f->brc.tid == tid) {
        PyMutex_Lock(&nb->mutex);
        runloom_brc_move_to_front(nb, &f->brc.bucket_node);
        PyMutex_Unlock(&nb->mutex);
        return;
    }
    {
        /* The fiber was last bound to another thread's bucket: move it.  Two
         * bucket mutexes, taken in address order so two hubs adopting across
         * each other's buckets cannot deadlock. */
        struct _brc_bucket *ob = runloom_brc_bucket(fiber->interp, f->brc.tid);
        struct _brc_bucket *first = ob < nb ? ob : nb, *second = ob < nb ? nb : ob;
        PyMutex_Lock(&first->mutex);
        if (second != first) PyMutex_Lock(&second->mutex);
        llist_remove(&f->brc.bucket_node);
        f->brc.tid = tid;
        llist_insert_tail(nb->root.next, &f->brc.bucket_node);
        if (second != first) PyMutex_Unlock(&second->mutex);
        PyMutex_Unlock(&first->mutex);
    }
}

/* Merges release() ran on the hub's stack, and how many of them found the stack
 * pointer outside the C-stack window of the state they ran under
 * (stats()["brc_release_merges"] / ["brc_release_merges_off_stack"]; the second
 * must stay 0, see release()). */
static unsigned long long runloom_brc_release_merges_total = 0;
static unsigned long long runloom_brc_release_merges_off_stack_total = 0;

unsigned long long runloom_iframe_brc_release_merges(void)
{
    return __atomic_load_n(&runloom_brc_release_merges_total, __ATOMIC_RELAXED);
}

unsigned long long runloom_iframe_brc_release_merges_off_stack(void)
{
    return __atomic_load_n(&runloom_brc_release_merges_off_stack_total,
                           __ATOMIC_RELAXED);
}

void runloom_iframe_brc_release(PyThreadState *fiber, PyThreadState *hub)
{
    _PyThreadStateImpl *f = (_PyThreadStateImpl *)fiber;
    _PyThreadStateImpl *h = (_PyThreadStateImpl *)hub;
    struct _brc_bucket *b = runloom_brc_bucket(fiber->interp, h->brc.tid);
    int pending;
    /* Parked: an id that is no OS thread's (the state's own address), see adopt. */
    fiber->thread_id = (unsigned long)(uintptr_t)fiber;
    fiber->native_thread_id = 0;
    PyMutex_Lock(&b->mutex);
    runloom_brc_move_to_front(b, &h->brc.bucket_node);
    /* Read under the bucket mutex: a dropper pushes under it and sets the
     * fiber's merge bit only AFTER releasing it, so the bit alone could miss
     * an object pushed just before we took the lock. */
    pending = (f->brc.objects_to_merge.head != NULL);
    PyMutex_Unlock(&b->mutex);
    if (pending) {
        /* The fiber's state is current on this thread, the owner of every
         * object queued to it (its tid is ours), and no fiber frame is
         * executing: a legitimate safe point for the eval loop's dispatcher.
         *
         * But this runs on the HUB's stack, and the fiber's C-stack limits
         * describe its own coroutine stack (runloom_coro_rearm_stackprot arms
         * them on every resume).  Every _Py_Dealloc in the merge measures the
         * stack pointer against them, and with the hub's SP below the fiber's
         * stack that margin comes out negative: each object is parked on the
         * fiber's trashcan list (delete_later) instead of freed, and only a
         * later dealloc on that state frees the list -- never, if the fiber
         * stops deallocating, since PyThreadState_Clear doesn't.  That leaked
         * a memoryview, and so pinned its array, in
         * test_memory_array_view_survives_a_migration whenever the stacks
         * happened to lie that way round.  Lend the fiber the hub's limits for
         * the merge (runloom_hub_main arms them), then put its own back. */
        uintptr_t top = f->c_stack_top, soft = f->c_stack_soft_limit;
        uintptr_t hard = f->c_stack_hard_limit;
        uintptr_t sp;
        f->c_stack_top = h->c_stack_top;
        f->c_stack_soft_limit = h->c_stack_soft_limit;
        f->c_stack_hard_limit = h->c_stack_hard_limit;
        sp = _Py_get_machine_stack_pointer();
        __atomic_add_fetch(&runloom_brc_release_merges_total, 1, __ATOMIC_RELAXED);
#if _Py_STACK_GROWS_DOWN
        if (sp < f->c_stack_hard_limit || sp > f->c_stack_top)
#else
        if (sp > f->c_stack_hard_limit || sp < f->c_stack_top)
#endif
            __atomic_add_fetch(&runloom_brc_release_merges_off_stack_total, 1,
                               __ATOMIC_RELAXED);
        _Py_set_eval_breaker_bit(fiber, _PY_EVAL_EXPLICIT_MERGE_BIT);
        (void)runloom_iframe_service_merge_queue(fiber);
        f->c_stack_top = top;
        f->c_stack_soft_limit = soft;
        f->c_stack_hard_limit = hard;
    }
}

/* Per-g states freed with deallocations still parked on their trashcan list
 * (stats()["fiber_trash_drained"]). */
static unsigned long long runloom_fiber_trash_drained_total = 0;

unsigned long long runloom_iframe_fiber_trash_drained(void)
{
    return __atomic_load_n(&runloom_fiber_trash_drained_total, __ATOMIC_RELAXED);
}

void runloom_iframe_drain_trashcan(PyThreadState *ts)
{
    if (ts == NULL || ts->delete_later == NULL) return;
    __atomic_add_fetch(&runloom_fiber_trash_drained_total, 1, __ATOMIC_RELAXED);
    _PyTrash_thread_destroy_chain(ts);
}

int runloom_tstate_in_destruction(PyThreadState *ts)
{
#if defined(RUNLOOM_DESTRUCT_HAVE)
    if (ts == NULL) {
        return 0;
    }
    /* Trashcan chain mid-unwind: objects whose tp_dealloc was deferred because
     * the C-recursion ran low are being destroyed by _PyTrash_thread_destroy_
     * chain.  Non-NULL for the whole unwind. */
    if (ts->delete_later != NULL) {
        return 1;
    }
    /* Biased-refcount cross-thread merge is draining: merge_queued_objects is
     * popping this per-thread stack and calling tp_dealloc (-> weakref
     * callbacks / finalizers) on each.  Non-empty => a destructor is in flight
     * on this tstate.  (objects_to_merge, the shared inbound queue, is NOT
     * checked: it only means work is *pending*, not that a destructor is
     * currently executing -- gating on it would needlessly throttle
     * preemption.) */
    if (((_PyThreadStateImpl *)ts)->brc.local_objects_to_merge.head != NULL) {
        return 1;
    }
    return 0;
#else
    (void)ts;
    return 0;
#endif
}

int runloom_iframe_walk(void *top, int max, runloom_iframe_cb cb, void *ctx)
{
#if defined(RUNLOOM_IFRAME_HAVE)
    _PyInterpreterFrame *f = (_PyInterpreterFrame *)top;
    int n = 0;
    while (f != NULL && n < max) {
        /* Skip the trampoline/shim frames that bracket a real call; they carry
         * no user code. */
#if PY_VERSION_HEX >= 0x030F0000
        /* 3.15 removed FRAME_OWNED_BY_CSTACK.  _PyFrame_IsIncomplete is CPython's
         * own "not a complete user frame" predicate (interpreter-entry sentinel +
         * not-yet-traceable shims) -- exactly what a traceback skips.  It tests
         * owner >= FRAME_OWNED_BY_INTERPRETER first, so _PyFrame_GetCode inside it
         * is only reached for real code-bearing frames. */
        if (!_PyFrame_IsIncomplete(f)) {
#else
        if (f->owner != FRAME_OWNED_BY_CSTACK) {
#endif
            /* f_executable is a tagged _PyStackRef, not a PyObject*. */
            PyObject *exec = PyStackRef_AsPyObjectBorrow(f->f_executable);
            if (exec != NULL && PyCode_Check(exec)) {
                int line = PyUnstable_InterpreterFrame_GetLine(f);
                if (cb((PyCodeObject *)exec, line, ctx) != 0)
                    return n;
                n++;
            }
        }
        f = f->previous;
    }
    return n;
#else
    (void)top; (void)max; (void)cb; (void)ctx;
    return 0;
#endif
}

/* Arm the live tstate's SP-based C-stack overflow check (3.14) at THIS fiber's
 * private mmap stack, with EXTRA reserved headroom above the hardware guard.
 *
 * 3.14 replaced the integer C-recursion counter with an SP check:
 * PyUnstable_ThreadState_SetStackProtection(ts, base, size) sets
 *   soft_limit = base + 2*MARGIN,  hard_limit = base + MARGIN
 * (stack grows down; _Py_MakeRecCheck raises RecursionError once SP descends
 * below soft_limit).  MARGIN is _PyOS_STACK_MARGIN_BYTES = 16 KB, so the default
 * arm leaves only 32 KB between the RecursionError trip point and the PROT_NONE
 * guard page.  On free-threaded 3.14 that 32 KB is NOT enough: once SP is just
 * below soft_limit a single deeper Python call runs CPython's datastack-chunk
 * path (_PyThreadState_PushFrame -> push_chunk -> allocate_chunk -> mmap, all on
 * the C stack) which itself consumes several KB and then writes into the new
 * chunk -- and that burst dips through the remaining margin into the guard page,
 * SIGSEGV (caught on p212: a fault in allocate_chunk on descent and a munmap on a
 * migrated unwind, both with SP within ~25-55 frames of soft_limit).  A bigger
 * fiber stack makes it WORSE (recursion runs deeper before the trip, more chunk
 * churn) -- proving the failure is the too-thin margin, not stack size.
 *
 * Fix: arm the check against a stack window that is RESERVE bytes SHORTER at the
 * low end -- pass base' = base + RESERVE so soft_limit = base + RESERVE + 2*MARGIN.
 * RecursionError then fires RESERVE earlier, leaving a comfortable cushion for the
 * chunk-alloc / frame-setup burst to complete above the guard.  RESERVE is a
 * fraction of the stack (so small fibers stay usable) with a floor sized to hold
 * the deepest single non-yielding CPython call burst, clamped so the window never
 * inverts on a tiny stack. */
#define RUNLOOM_STACKPROT_RESERVE_MIN ((size_t)96 * 1024)   /* >= one chunk-alloc burst */

/* PyUnstable_ThreadState_SetStackProtection fails (-1, ValueError set on the
 * CURRENT tstate) only for a window below _PyOS_MIN_STACK_SIZE -- and that
 * minimum belongs to the INTERPRETER's build, not to this extension's headers:
 * a TSan-built CPython raises it from 48 KB to 192 KB, so the reserved window of
 * a 256 KB fiber stack (160 KB) is refused there.  Ignoring the -1 left the
 * ValueError pending, and the fiber's first Python call then failed with
 * "SystemError: ... returned a result with an exception set".  Clear it and
 * report failure so the caller can fall back. */
static int runloom_set_stackprot(PyThreadState *ts, void *base, size_t size)
{
    if (PyUnstable_ThreadState_SetStackProtection(ts, base, size) == 0)
        return 0;
    PyErr_Clear();
    return -1;
}

void runloom_arm_fiber_stackprot(PyThreadState *ts, runloom_coro_t *c)
{
    void  *base;
    size_t size, reserve, eff;
    if (ts == NULL || c == NULL) return;
    base = runloom_coro_stack_base(c);
    size = runloom_coro_stack_size(c);
    if (base == NULL || size == 0) return;
    /* Reserve max(min, size/8), but never more than half the stack so the usable
     * window can't collapse on a small fiber. */
    reserve = size / 8;
    if (reserve < RUNLOOM_STACKPROT_RESERVE_MIN) reserve = RUNLOOM_STACKPROT_RESERVE_MIN;
    if (reserve > size / 2) reserve = size / 2;
    eff = size - reserve;
    if (eff >= RUNLOOM_STACKPROT_RESERVE_MIN
        && runloom_set_stackprot(ts, (void *)((char *)base + reserve), eff) == 0)
        return;
    /* Stack too small to reserve usefully (or the reserved window is below the
     * interpreter's minimum): raw arm against the real geometry -- still bounds
     * the check, better than leaving it stale.  If even that is refused the old
     * limits stay, as before, but no exception leaks. */
    (void)runloom_set_stackprot(ts, base, size);
}

/* offsetof(PyGenObject, gi_exc_state) -- computed in THIS Py_BUILD_CORE-isolated TU,
 * the only one that sees the complete _PyGenObject (on 3.14 the struct moved into
 * pycore_interpframe_structs.h; gi_exc_state is macro-generated by _PyGenObject_HEAD,
 * present on 3.13 and 3.14).  runloom_sched's exc-chain pinning calls this so it needs
 * no internal headers of its own. */
size_t runloom_gen_exc_state_offset(void)
{
    return offsetof(PyGenObject, gi_exc_state);
}

/* ---- critical-section suspend/restore across a fiber swap ----
 * See the header for why this is needed.  Mirrors what CPython does in
 * _PyThreadState_Detach / _Attach, but driven manually at runloom's park
 * boundary (runloom never detaches the tstate on a cooperative park). */
uintptr_t runloom_critsec_suspend(void *tstate_v)
{
#if defined(RUNLOOM_CRITSEC_HAVE)
    PyThreadState *ts = (PyThreadState *)tstate_v;
    uintptr_t saved = ts->critical_section;
    if (saved != 0) {
        /* Unlocks every CS mutex held on this tstate and tags the chain
         * inactive (chain pointer stays in ts->critical_section). */
        _PyCriticalSection_SuspendAll(ts);
        saved = ts->critical_section;   /* re-read: now tagged inactive */
        ts->critical_section = 0;       /* hand the next fiber a clean chain */
    }
    return saved;
#else
    (void)tstate_v;
    return 0;
#endif
}

void runloom_critsec_restore(void *tstate_v, uintptr_t saved)
{
#if defined(RUNLOOM_CRITSEC_HAVE)
    if (saved != 0) {
        PyThreadState *ts = (PyThreadState *)tstate_v;
        ts->critical_section = saved;
        /* Re-lock the top section (it was tagged inactive by SuspendAll).
         * Nested inner sections stay inactive until popped, each Pop resuming
         * the next -- exactly CPython's attach-time behaviour. */
        if (!_PyCriticalSection_IsActive(saved)) {
            _PyCriticalSection_Resume(ts);
        }
    }
#else
    (void)tstate_v; (void)saved;
#endif
}

/* ---- c_stack_refs take/set across a fiber swap (free-threaded 3.14+) ----
 *
 * 3.14 free-threaded builds added _PyThreadStateImpl.c_stack_refs: the head of a
 * singly-linked list of _PyCStackRef nodes, each living on the C STACK of the
 * function that pushed it (eval loop / C-API steal paths hold a temporary
 * borrowed reference there).  The free-threaded GC walks this list per thread
 * state in gc_visit_thread_stacks() to count deferred references.
 *
 * In runloom's default mode many fibers share ONE per-hub PyThreadState but each
 * fiber has its OWN mmap'd C stack.  A fiber can park (cooperatively yield)
 * while a _PyCStackRef node it pushed is still live on its stack and linked into
 * the shared tstate's c_stack_refs.  The next fiber resumed on that same hub
 * then pushes/pops its own nodes (on ITS stack) onto the SAME list head, and its
 * stack activity overwrites the parked fiber's node bytes -- so the list's `next`
 * pointers come to thread across two unrelated C stacks, some since reused.  When
 * the GC (gc.collect()) later walks c_stack_refs it follows a `next` into stale
 * memory -> SIGSEGV (the p77_weakref_storm crash: gc_visit_thread_stacks faulting
 * with a node whose next pointed into the code segment).
 *
 * Fix (mirrors current_frame / datastack_chunk privatisation): take() saves the
 * head and clears it so a sibling fiber starts with an empty, private list; set()
 * restores this fiber's own head on resume.  Each fiber's c_stack_refs list then
 * lives entirely on its own preserved stack, exactly like its frame chain.
 * Returned/passed as void* to keep the core/non-core ABI boundary clean. */
void *runloom_tstate_take_cstack_refs(void *tstate_v)
{
    _PyThreadStateImpl *ts = (_PyThreadStateImpl *)tstate_v;
    void *head = (void *)ts->c_stack_refs;
    ts->c_stack_refs = NULL;          /* hand the next fiber a clean list */
    return head;
}

void runloom_tstate_set_cstack_refs(void *tstate_v, void *head)
{
    _PyThreadStateImpl *ts = (_PyThreadStateImpl *)tstate_v;
    ts->c_stack_refs = (_PyCStackRef *)head;
}

/* ---- GC visibility for parked-fiber frames (free-threaded 3.14+) ----
 * See runloom_iframe.h and module_gcframes.c.inc for the why.  The per-frame
 * visit set is transcribed from greenlet PR #511 (PythonState::tp_traverse,
 * GREENLET_PY315) + CPython's _PyGC_VisitStackRef / _PyGC_VisitFrameStack
 * (Python/gc_free_threading.c), with the collector's static visit_decref
 * comparison replaced by the caller's `subtract` flag (which the anchor derives
 * from its own _PyGC_BITS_UNREACHABLE bit -- see runloom_gc_in_subtract_pass).
 * NONE of this may allocate or free: the subtract-pass call site (update_refs)
 * runs inside the collector's heap walk where touching mimalloc is forbidden. */
#if defined(RUNLOOM_GCFRAMES_HAVE)

int runloom_gc_world_stopped(void)
{
    PyInterpreterState *interp = _PyInterpreterState_GET();
    return interp != NULL && interp->stoptheworld.world_stopped;
}

/* During update_refs the collector runs gc_maybe_init_refs(op) -- which SETS op's
 * _PyGC_BITS_UNREACHABLE -- immediately before tp_traverse(op, visit_decref, NULL).
 * Every PROPAGATION traverse runs with the bit CLEAR: mark-alive runs before any
 * bits are set and marks ALIVE before traversing; mark_heap_visitor clears
 * UNREACHABLE before mark_reachable; visit_clear_unreachable clears it before
 * pushing; visit_decref_unreachable traverses only unreachable-worklist objects,
 * which the always-rooted anchor can never be (its hidden C-global reference keeps
 * gc_refs >= 1).  Hence, FOR THE ANCHOR OBJECT ONLY, the bit is set at traverse
 * time IFF this is the subtract pass.  3.14.x-only; 3.15+ uses the exported
 * visitors, which self-discriminate. */
int runloom_gc_in_subtract_pass(PyObject *self)
{
    return self != NULL && (self->ob_gc_bits & _PyGC_BITS_UNREACHABLE) != 0;
}

/* One tagged stackref.  Mirrors _Py_VISIT_STACKREF (skip NullOrInt first: the
 * collector asserts !IsTaggedInt) + the body of _PyGC_VisitStackRef: a deferred
 * reference is NOT part of the refcount (update_refs already stripped the
 * deferred bias), so the SUBTRACT pass must skip it -- visiting would
 * double-subtract and free a LIVE object -- while every other pass treats it as a
 * regular reference so propagation from the anchor keeps its referent alive.
 *
 * 3.15 exports _PyGC_VisitStackRef, which performs exactly this discrimination
 * itself -- keyed on the visitproc identity (visit_decref / visit_decref_unreachable
 * ARE the subtract pass) rather than a caller-supplied flag.  So on 3.15+ we defer
 * to it: the `subtract` argument is unused, and PyStackRef_IsDeferred (used by the
 * 3.14 branch) was removed in favour of that self-discrimination.  We still guard
 * NullOrInt here, exactly as the _Py_VISIT_STACKREF macro does before calling. */
static int runloom_visit_stackref(_PyStackRef *ref, visitproc visit, void *arg,
                                  int subtract)
{
    if (PyStackRef_IsNullOrInt(*ref)) {
        return 0;
    }
#if PY_VERSION_HEX >= 0x030F0000
    (void)subtract;
    return _PyGC_VisitStackRef(ref, visit, arg);
#else
    if (subtract && PyStackRef_IsDeferred(*ref)) {
        return 0;
    }
    {
        PyObject *op = PyStackRef_AsPyObjectBorrow(*ref);
        if (op != NULL) {
            return visit(op, arg);
        }
    }
    return 0;
#endif
}

int runloom_gcvisit_frame_chain(void *top, visitproc visit, void *arg, int subtract)
{
    _PyInterpreterFrame *f = (_PyInterpreterFrame *)top;
    for (; f != NULL; f = f->previous) {
        int r;
        _PyStackRef *ref, *sp;
        /* Visit only thread-owned frames.  Generator/coroutine frames (owner ==
         * FRAME_OWNED_BY_GENERATOR) are embedded in their independently GC-tracked
         * gen objects, whose gen_traverse already visits them with per-visitor
         * semantics -- visiting them here too would DOUBLE-SUBTRACT their counted
         * stackrefs in update_refs.  FRAME_OWNED_BY_FRAME_OBJECT is likewise
         * handled once by frame_traverse; INTERPRETER/CSTACK shim frames carry no
         * user references.  Mirrors greenlet PR #511's owner filter exactly. */
        if (f->owner != FRAME_OWNED_BY_THREAD) {
            continue;
        }
        if (f->frame_obj != NULL) {                     /* strong ref; every pass */
            r = visit((PyObject *)f->frame_obj, arg);   if (r) return r;
        }
        if (f->f_locals != NULL) {                      /* strong ref; every pass */
            r = visit(f->f_locals, arg);                if (r) return r;
        }
        r = runloom_visit_stackref(&f->f_funcobj,    visit, arg, subtract); if (r) return r;
        r = runloom_visit_stackref(&f->f_executable, visit, arg, subtract); if (r) return r;
        ref = _PyFrame_GetLocalsArray(f);               /* == f->localsplus */
        sp  = f->stackpointer;
        if (sp == NULL) {
            /* GH-129236 shape: a frame caught mid-PyStackRef_CLOSE has an
             * un-synced stackpointer.  It should NOT occur in a PARKED chain
             * (cooperative parks and preempt-yields suspend at call boundaries
             * where the eval loop has synced the stackpointer -- asserted at snap,
             * SECTION 3).  Skip the variable window defensively; the fixed fields
             * above are already visited.  A stronger conservative handling (force
             * the collector's skip_deferred_objects for that cycle) is a
             * follow-up (design AM-1). */
            continue;
        }
        for (; ref < sp; ref++) {
            r = runloom_visit_stackref(ref, visit, arg, subtract);
            if (r) return r;
        }
    }
    return 0;
}

int runloom_gcvisit_cstack_chain(void *head, visitproc visit, void *arg, int subtract)
{
    _PyCStackRef *c = (_PyCStackRef *)head;
    for (; c != NULL; c = c->next) {
        int r = runloom_visit_stackref(&c->ref, visit, arg, subtract);
        if (r) return r;
    }
    return 0;
}

/* AM-8: keep the anchor un-frozen.  gc.freeze() stamps EVERY tracked object with
 * _PyGC_BITS_FROZEN, and the free-threaded collector skips frozen objects in its
 * heap walk / mark passes -- so once frozen, the anchor's tp_traverse would stop
 * running and parked-fiber frames would silently become invisible again,
 * reopening the crash for any deferred-only referent created AFTER the freeze
 * (which is itself not frozen and thus collectible).  A gc "start" callback calls
 * this each collection to clear the bit, so the anchor always participates.
 * (greenlet 3.15 has the same freeze hole; this closes it for runloom.)  No-op if
 * the object is not frozen. */
void runloom_gc_anchor_keep_thawed(PyObject *op)
{
    if (op != NULL && _PyObject_HAS_GC_BITS(op, _PyGC_BITS_FROZEN)) {
        _PyObject_CLEAR_GC_BITS(op, _PyGC_BITS_FROZEN);
    }
}

#else   /* RUNLOOM_NO_IFRAME: safe stubs so callers stay unconditional */

int runloom_gc_world_stopped(void) { return 0; }
int runloom_gc_in_subtract_pass(PyObject *self) { (void)self; return 0; }
int runloom_gcvisit_frame_chain(void *top, visitproc visit, void *arg, int subtract)
{ (void)top; (void)visit; (void)arg; (void)subtract; return 0; }
int runloom_gcvisit_cstack_chain(void *head, visitproc visit, void *arg, int subtract)
{ (void)head; (void)visit; (void)arg; (void)subtract; return 0; }
void runloom_gc_anchor_keep_thawed(PyObject *op) { (void)op; }

#endif  /* RUNLOOM_GCFRAMES_HAVE */
