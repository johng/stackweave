/* module.c -- Python bindings for runloom_c.
 *
 * Exposes:
 *   runloom_c.Coro(callable, stack_size=131072) -> coro object
 *      .resume()         switch into the coroutine
 *      .done             True if entry returned
 *   runloom_c.yield_()   yield from inside a coroutine
 *   runloom_c.backend()  "fcontext-asm" | "ucontext"
 *
 * Free-threaded friendly: each OS thread runs its own coroutines.
 * We do NOT release the GIL during resume() because the Python callable
 * we run inside the coro will reacquire/release as it pleases.  Under
 * free-threaded Python there is no global lock to release.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include "plat.h"
#include "plat_compat.h"
#include "coro.h"
#include "runloom_iframe.h"   /* runloom_arm_fiber_stackprot (3.14 SP-check arm) */
#include "runloom_sched.h"
#include "runloom_cover.h"   /* Sometimes() reachability accessors */
#include "netpoll.h"
#include "io_uring.h"   /* runloom_iouring_cancel_g for the cancel path */
#include <stdlib.h>   /* getenv -- the test-only fd-fault guard below */
#include "mn_sched.h"
#include "chan.h"
#include "runloom_tcp.h"
#include "runloom_io_fsm.h"   /* the total (rc,errno)->event I/O classifier */
#include "runloom_blockpool.h"
#include "runloom_diag.h"
#include "runloom_gstate.h"
#include "runloom_introspect.h"
#include "runloom_crash.h"
#include "runloom_stackadvice.h"

/* ---- Per-coro Python object ---- */

/* CPython thread-state snapshot.  These fields are not preserved by a
 * raw C-stack swap, but the Python frame chain + recursion counters live
 * on the thread state and need to follow the coroutine.  We snapshot
 * what we can portably:
 *  - py_recursion_remaining
 *  - current_frame, the topmost interpreter frame (a direct PyThreadState
 *    member).  This one is load-bearing, not just a
 *    counter: resume() MUST leave ts->current_frame exactly as it found it
 *    (the caller's frame).  When a coro parks at yield_ it has pushed its
 *    own frame onto ts->current_frame; if resume() returns without putting
 *    the caller's frame back, ts->current_frame dangles into the coro.  A
 *    later destroy of an unfinished coro then frees that frame's backing
 *    store, and the free-threaded GC's thread-stack walk
 *    (gc_visit_thread_stacks_mark_alive) follows the stale ->previous chain
 *    into freed memory and spins forever.  Saving/restoring it keeps the
 *    coro's live frames off the caller's chain.
 *    The coro's chain is linked to its resumer as a generator's is: each
 *    resume points the coro's bottom frame at the resumer's frame, and each
 *    yield cuts that link (runloom_coro_chain_cut/_link).  Left linked to
 *    the frame that first resumed it, the chain dangled once that frame
 *    returned: a later resume from elsewhere that raised, walked f_back, or
 *    ran a collection followed it into freed datastack memory and SIGSEGVed.
 *    A parked coro's frames are then on no thread's chain, so the GC frames
 *    anchor visits them (`parked`, module_gcframes.c.inc); invisible, a
 *    collection freed objects only their deferred references kept alive
 *    (its function and code), and the resume ran freed bytecode.
 *  - the C-stack limits (3.14).  Every resume arms them at the coro's stack
 *    (runloom_coro_rearm_stackprot); unrestored, the caller ran on under
 *    them, and deep recursion or a deep free there crashed off the end of
 *    its own stack instead of raising RecursionError.  Only caller_snap's
 *    copy matters: the re-arm overwrites the coro's own before it runs.
 *  - the critical-section chain and the c_stack_refs list (free-threaded),
 *    swapped exactly as the scheduler's snap does for a fiber.  Both are
 *    linked lists of nodes on the C stack that owns them, headed in the
 *    thread state.  Left shared, a coro parked inside list(map())'s critical
 *    section left its node at the head of the caller's chain (and kept the
 *    list's mutex locked); the next Coro to push or the caller to pop then
 *    linked through the other's stack, and two Coros taking turns that way
 *    SIGSEGVed.  Saving suspends the chain (unlocking its mutexes) and takes
 *    the list; restoring re-locks and puts them back.
 *  - the datastack (the chunks the interpreter pushes Python frames onto).
 *    A coro starts with none (Coro.resume), so CPython allocates its first
 *    chunk on the first push, as for a new thread, and frees the coro's chunks
 *    to the arena when it finishes (runloom_coro_free_datastack) -- never
 *    through the scheduler's per-thread chunk pool, which a plain thread
 *    never flushes.  On one shared datastack, Coros taking turns interleaved
 *    their frames, and the first to return popped the top back below frames
 *    another still owned, which the next push overwrote: eight Coros
 *    recursing 50 Python levels deep, taking turns, SIGSEGVed. */
typedef struct {
    int py_recursion_remaining;
    struct _PyInterpreterFrame *current_frame;
    runloom_cstack_limits_t c_stack;
    uintptr_t critical_section;
    void *c_stack_refs;
    _PyStackChunk *datastack_chunk;
    PyObject **datastack_top;
    PyObject **datastack_limit;
    int initialised;
    /* A coro's own snapshot only.  bottom_frame: the frame whose link to the
     * resumer was cut at the last yield (NULL when no frame was live).
     * bottom_fixed: the coro's callable is a Python function, so its one
     * outermost eval call -- whose entry frame is bottom_frame -- lasts from
     * the first resume until the coro finishes, and later yields cut there
     * without walking the chain.  parked: 1 from a yield until the next
     * resume while current_frame and c_stack_refs are the coro's alone; the
     * GC frames anchor visits them then.  Set last at a yield and cleared
     * first at a resume, with no safepoint between, like a fiber snap's
     * `valid`. */
    struct _PyInterpreterFrame *bottom_frame;
    int bottom_fixed;
    int parked;
} RunloomTstateSnapshot;

typedef struct {
    PyObject_HEAD
    runloom_coro_t *coro;
    PyObject *callable;   /* invoked once when the coro first resumes */
    PyObject *result;     /* return value of callable, or NULL */
    PyObject *error;      /* unhandled exception caught, or NULL */
    int has_run;
    int executing;        /* 1 while inside runloom_coro_resume (re-entrancy guard) */
    RunloomTstateSnapshot tstate_snap;  /* captured at yield, restored at resume */
    /* tstate_snap on the base-snap registry, for the GC frames anchor: linked
     * at the first yield, unlinked when the coro finishes or is dropped. */
    runloom_base_snap_node_t gc_node;
    int gc_linked;
} RunloomCoro;

/* Parked coros the GC frames anchor has walked (stats()["gc_coros_walked"]).
 * Bumped by the anchor under stop-the-world, so a plain counter. */
static unsigned long long runloom_gcframes_walked_coros = 0;

RUNLOOM_INLINE void runloom_tstate_save(RunloomTstateSnapshot *s)
{
    PyThreadState *ts = PyThreadState_GET();
    s->py_recursion_remaining = ts->py_recursion_remaining;
    s->current_frame = ts->current_frame;
    runloom_cstack_limits_save(ts, &s->c_stack);
    s->critical_section = runloom_critsec_suspend(ts);
    s->c_stack_refs = runloom_tstate_take_cstack_refs(ts);
    s->datastack_chunk = ts->datastack_chunk;
    s->datastack_top = ts->datastack_top;
    s->datastack_limit = ts->datastack_limit;
    s->initialised = 1;
}

/* A coro dropped (dealloc / re-init) while parked.  Its frames are abandoned
 * unpopped, as before, and so are its datastack chunks: a frame object made
 * while it ran (a kept traceback, sys._getframe()) can still point into them,
 * so reusing the memory would turn a later read of that frame into a
 * use-after-free.  The scheduler leaves a destroyed parked fiber's chunks the
 * same way.  Its critical sections were unlocked when it parked, and its
 * c_stack_refs nodes die with its stack.  The coro must be off the base-snap
 * registry first (runloom_coro_gc_forget): its bottom frame is on the C stack
 * the caller is about to free. */
RUNLOOM_INLINE void runloom_tstate_snap_drop(RunloomTstateSnapshot *s)
{
    if (s->initialised) {
        s->datastack_chunk = NULL;
        s->datastack_top = s->datastack_limit = NULL;
        s->critical_section = 0;
        s->c_stack_refs = NULL;
    }
    s->bottom_frame = NULL;
    s->parked = 0;
    s->initialised = 0;
}

/* At a yield, before the coro's state is saved: cut its frames off the
 * resumer's, `caller` (runloom_iframe_chain_cut).  With no frame of its own
 * live (a C callable that yielded outside any Python call) the saved chain is
 * empty.  Returns 0, leaving the chain as it is, if `caller` is not on it,
 * which no yield inside Coro.resume makes; the snapshot is then not shown to
 * the GC.  The walk is the coro's frame depth; a bottom_fixed coro takes it
 * at its first yield only. */
RUNLOOM_INLINE int runloom_coro_chain_cut(RunloomTstateSnapshot *s,
                                          struct _PyInterpreterFrame *caller)
{
    PyThreadState *ts = PyThreadState_GET();
    if (s->bottom_fixed && s->bottom_frame != NULL) {
        runloom_iframe_chain_link(s->bottom_frame, NULL);
        return 1;
    }
    if (ts->current_frame == caller) {
        ts->current_frame = NULL;
        s->bottom_frame = NULL;
        return 1;
    }
    s->bottom_frame = (struct _PyInterpreterFrame *)
        runloom_iframe_chain_cut(ts->current_frame, caller);
    return s->bottom_frame != NULL;
}

/* At a resume, before the coro's state is restored: hide it from the GC (its
 * frames are about to be on the live chain) and link its chain to the new
 * resumer's frame. */
RUNLOOM_INLINE void runloom_coro_chain_link(RunloomTstateSnapshot *s,
                                            struct _PyInterpreterFrame *caller)
{
    s->parked = 0;
    if (s->bottom_frame != NULL)
        runloom_iframe_chain_link(s->bottom_frame, caller);
    else if (s->current_frame == NULL)
        s->current_frame = caller;
}

/* A coro finished: every frame is popped (a frame object that outlives its
 * frame has taken a copy), so its chunks are free.  The root goes into the
 * thread state's one-chunk cache when that is empty -- CPython's own
 * push_chunk takes it for the next coro's first frame, and clearing the
 * thread state frees it -- so a coro doesn't cost an mmap + munmap (8x on
 * create-and-run); the rest go to the arena, as CPython frees a popped
 * chunk. */
RUNLOOM_INLINE void runloom_coro_free_datastack(void)
{
    PyThreadState *ts = PyThreadState_GET();
    _PyStackChunk *chunk = ts->datastack_chunk;
    PyObjectArenaAllocator alloc;
    ts->datastack_chunk = NULL;
    ts->datastack_top = NULL;
    ts->datastack_limit = NULL;
    if (chunk == NULL) return;
    PyObject_GetArenaAllocator(&alloc);
    while (chunk != NULL) {
        _PyStackChunk *prev = chunk->previous;
        if (prev == NULL && ts->datastack_cached_chunk == NULL) {
            chunk->top = 0;
            ts->datastack_cached_chunk = chunk;
        } else {
            alloc.free(alloc.ctx, chunk, chunk->size);
        }
        chunk = prev;
    }
}

RUNLOOM_INLINE void runloom_tstate_restore(RunloomTstateSnapshot *s)
{
    PyThreadState *ts;
    if (!s->initialised) {
        return;
    }
    ts = PyThreadState_GET();
    ts->py_recursion_remaining = s->py_recursion_remaining;
    ts->current_frame = s->current_frame;
    runloom_cstack_limits_restore(ts, &s->c_stack);
    ts->datastack_chunk = s->datastack_chunk;
    ts->datastack_top = s->datastack_top;
    ts->datastack_limit = s->datastack_limit;
    runloom_tstate_set_cstack_refs(ts, s->c_stack_refs);
    s->c_stack_refs = NULL;
    runloom_critsec_restore(ts, s->critical_section);
    s->critical_section = 0;
}


/* runloom_coro_pre_swap hook (registered in PyInit on free-threaded 3.14+):
 * re-arm the live thread state's SP-based C-stack overflow check at THIS fiber's
 * stack on every resume.  3.14 replaced the integer recursion counter with an
 * SP-vs-soft_limit check; because the single-thread scheduler's fibers share one
 * tstate, the limit set at a fiber's entry is overwritten by the next fiber to
 * enter -- so a parked-then-resumed deep recurser would run off its own
 * stack into the guard page (SIGSEGV).  Re-reading base+size each resume also
 * tracks runloom_coro_maybe_grow's copy-grow, when it is turned on.
 *
 * Delegates to runloom_arm_fiber_stackprot (runloom_iframe.c), which reserves
 * extra headroom above the guard so the RecursionError trips before CPython's
 * datastack-chunk-alloc burst can dip into the guard page (the p212 fix). */
static void runloom_coro_rearm_stackprot(runloom_coro_t *c)
{
    PyThreadState *ts = PyThreadState_GetUnchecked();
    if (ts != NULL && c != NULL)
        runloom_arm_fiber_stackprot(ts, c);
}

/* ---------------------------------------------------------------------------
 * module.c is split across the module_*.c.inc fragments below for readability.
 * They are #included here (one translation unit): the fragments share this
 * file's includes, typedefs and file-scope statics and are NOT compiled
 * standalone.  setup.py compiles only module.c.
 * --------------------------------------------------------------------------- */
#include "module_coro.c.inc"
#include "module_tcp.c.inc"
#include "module_io.c.inc"
#include "module_fdio.c.inc"
#include "module_g.c.inc"
#include "module_chan.c.inc"
#include "module_fiber.c.inc"
#include "module_run.c.inc"
#include "module_introspect.c.inc"
#include "module_crash.c.inc"
#include "module_advice.c.inc"
#include "module_select.c.inc"
#include "module_machinecode.c.inc"
#include "module_gcframes.c.inc"
#include "module_init.c.inc"
