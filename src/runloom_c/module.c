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
 *    coro's live frames off the caller's chain (the GC descends only via
 *    ->previous from current_frame, so a parked coro's frames become
 *    invisible while still resident in the shared datastack for a later
 *    resume).  We restore only the topmost pointer, never datastack_top --
 *    reclaiming the coro's frame slots would let the caller overwrite a
 *    still-parked coro.
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
 *  - the datastack (the chunk the interpreter pushes Python frames onto),
 *    also as the scheduler's snap does.  A coro gets chunks of its own on its
 *    first resume (Coro.resume) and gives them back when it finishes.  On one
 *    shared datastack, Coros taking turns interleaved their frames, and the
 *    first to return popped the top back below frames another still owned,
 *    which the next push overwrote: eight Coros recursing 50 Python levels
 *    deep, taking turns, SIGSEGVed. */
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
} RunloomCoro;

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

/* A coro dropped (dealloc / re-init) while parked: give back the datastack
 * chunks it owned.  Its frames are abandoned unpopped, as before; its critical
 * sections were already unlocked when it parked, and its c_stack_refs nodes
 * die with its stack. */
RUNLOOM_INLINE void runloom_tstate_snap_drop(RunloomTstateSnapshot *s)
{
    if (s->initialised) {
        runloom_datastack_release(s->datastack_chunk);
        s->datastack_chunk = NULL;
        s->datastack_top = s->datastack_limit = NULL;
        s->critical_section = 0;
        s->c_stack_refs = NULL;
    }
    s->initialised = 0;
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
 * SP-vs-soft_limit check; because the default mode shares one tstate across all
 * fibers on a hub, the limit set at a fiber's entry is overwritten by the next
 * fiber to enter -- so a parked-then-resumed deep recurser would run off its own
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
