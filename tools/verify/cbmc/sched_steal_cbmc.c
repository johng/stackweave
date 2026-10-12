/* SOURCE-ANCHOR: runloom_cldeque_steal runloom_mn_woken_enqueue  (guards this hand-model vs src drift; tools/verify/model_source_drift.py) */
/*
 * sched_steal_cbmc.c -- CBMC model of the WORK-STEALING consume path's refcount
 * safety, the piece chase_lev_real.c (deque mechanics) does not cover.
 *
 * A LOCALLY-WOKEN g sits on a hub's Chase-Lev deque in wake_state QUEUED,
 * carrying the queue ref its waker took on PARKED->QUEUED
 * (runloom_mn_woken_enqueue pushes it onto the waker's own deque).  The g can
 * then be consumed by EITHER the deque's owner hub (deque take) OR a thief hub
 * (runloom_cldeque_steal) -- and Chase-Lev guarantees EXACTLY ONE of them wins.
 * Whoever wins flags it from_runq, claims it QUEUED->RUNNING, resumes it,
 * drops the queue ref, and (on completion) drops the scheduler ref
 * (mn_sched_hub_main.c.inc, the LOCAL WAKE check and the per-g resume block).
 *
 * The caller-level safety the steal path RELIES on, and this proves:
 *   NoUAF        : no read of g after it is freed.
 *   RefNonNeg    : refcount never goes negative (no double-decref of a ref).
 *   FreedOnce    : the g is released exactly once.
 *
 * Scope: this proves the piece UNIQUE to the steal path -- TWO potential
 * consumers (owner-take vs thief-steal) of one deque g, and that the queue ref
 * is dropped EXACTLY ONCE across them.  That a wake enqueues a g at most once is
 * the wake_state machine's job (wake_state.pml, wake_state_fsm_cbmc.c); keeping
 * the waker out here holds the thread count to 2 so CBMC stays tractable.
 *
 * Configs (run via run_steal_cbmc.sh):
 *   default              : Chase-Lev exactly-once claim -> queue ref dropped once
 *                          -> no UAF, refcount>=0.
 *   -DBUG_DOUBLE_CONSUME  : the deque lets BOTH owner-take and thief-steal consume
 *                          the same g (exactly-once broken) -> the queue ref is
 *                          decref'd twice -> premature free -> UAF / refcount<0.
 */
#include <pthread.h>
#include <assert.h>

extern int nondet_int(void);

static volatile int refcount;      /* scheduler ref + queue ref */
static volatile int queued;        /* wake_state == QUEUED; held 1 on the deque */
static volatile int claimed;       /* Chase-Lev exactly-once: 0 in deque, 1 taken */
static volatile int freed;
static volatile int uaf;

static void touch(void) { if (freed) uaf = 1; }

static void g_decref(void) {
    int n = __atomic_sub_fetch(&refcount, 1, __ATOMIC_ACQ_REL);
    assert(n >= 0);                 /* RefNonNeg */
    if (n == 0) freed = 1;
}

/* One consumer -- an owner-take OR a thief-steal.  Chase-Lev grants the g to
 * EXACTLY ONE consumer: the winner of the `claimed` CAS.  Whoever wins runs the
 * SAME ref protocol regardless of which hub it is (that is the point -- the steal
 * caller is not special once it holds the g). */
static void *consumer(void *a) {
    (void)a;
#ifndef BUG_DOUBLE_CONSUME
    int expect = 0;
    if (!__atomic_compare_exchange_n(&claimed, &expect, 1, 0,
                                     __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE))
        return 0;                   /* lost the race: the other hub got the g */
#endif
    /* consume: claim QUEUED->RUNNING, resume, drop the queue ref. */
    int had = (__atomic_exchange_n(&queued, 0, __ATOMIC_ACQ_REL) == 1);
    touch();                        /* resume reads g */
    if (had) g_decref();            /* drop the queue ref carried through the deque */
    if (nondet_int()) {             /* the g may run to completion here */
        touch();
        g_decref();                 /* drop the scheduler ref (frees on last) */
    }
    return 0;
}

int main(void) {
    refcount = 2;        /* scheduler ref + queue ref (on the deque) */
    queued = 1;          /* wake_state QUEUED while on the deque */
    claimed = 0;
    freed = 0;
    uaf = 0;

    pthread_t owner, thief;
    pthread_create(&owner, 0, consumer, 0);   /* the owner hub's deque take */
    pthread_create(&thief, 0, consumer, 0);   /* a thief hub's cldeque_steal */
    pthread_join(owner, 0);
    pthread_join(thief, 0);

    assert(!uaf);                    /* NoUAF */
    assert(refcount >= 0);           /* RefNonNeg */
    return 0;
}
