/* plat_compat.h -- thin runtime wrappers the rest of runloom_c uses across
 * POSIX (Linux / macOS / BSD / Solaris).
 *
 * Groups:
 *   1. mutex          runloom_mutex_t + init/lock/unlock/destroy
 *   2. thread         runloom_thread_t + create/join
 *   3. condvar        runloom_cond_t + wait/timedwait/signal/broadcast
 *   4. clock & sleep  runloom_monotonic_ns / runloom_sleep_ns
 *   5. CPU count      runloom_cpu_count
 *
 * Each maps to pthread / clock_gettime / nanosleep / sysconf.
 *
 * Header-only: every function is `static inline` so consumers don't link
 * an extra TU.  RUNLOOM_INLINE is defined in plat.h to expand to whatever
 * the compiler accepts.
 */
#ifndef RUNLOOM_PLAT_COMPAT_H
#define RUNLOOM_PLAT_COMPAT_H

#include "plat.h"

#include <stdint.h>

#include <pthread.h>
#include <time.h>
#include <errno.h>
#if defined(RUNLOOM_OS_LINUX) || defined(RUNLOOM_OS_BSD) || defined(RUNLOOM_OS_MACOS) \
    || defined(RUNLOOM_OS_SOLARIS) || defined(RUNLOOM_OS_ANDROID)
#  include <unistd.h>
#endif

/* ============================================================ */
/* mutex                                                        */
/* ============================================================ */
typedef pthread_mutex_t runloom_mutex_t;
RUNLOOM_INLINE int  runloom_mutex_init(runloom_mutex_t *m) {
    return pthread_mutex_init(m, NULL);
}
RUNLOOM_INLINE void runloom_mutex_destroy(runloom_mutex_t *m) {
    pthread_mutex_destroy(m);
}
RUNLOOM_INLINE void runloom_mutex_lock(runloom_mutex_t *m) {
    pthread_mutex_lock(m);
}
/* 0 = acquired, nonzero = contended (would block).  Used by the
 * signal-safe fiber dump so a contended lock degrades to a partial
 * dump instead of deadlocking a SIGQUIT handler. */
RUNLOOM_INLINE int runloom_mutex_trylock(runloom_mutex_t *m) {
    return pthread_mutex_trylock(m);
}
RUNLOOM_INLINE void runloom_mutex_unlock(runloom_mutex_t *m) {
    pthread_mutex_unlock(m);
}
#define RUNLOOM_MUTEX_STATIC_INIT  PTHREAD_MUTEX_INITIALIZER

/* ============================================================ */
/* thread                                                       */
/* ============================================================ */
typedef pthread_t runloom_thread_t;
typedef void *(*runloom_thread_fn)(void *);

RUNLOOM_INLINE int runloom_thread_create(runloom_thread_t *t,
                                   runloom_thread_fn fn,
                                   void *arg) {
    return pthread_create(t, NULL, fn, arg);
}
RUNLOOM_INLINE int runloom_thread_join(runloom_thread_t t) {
    return pthread_join(t, NULL);
}
#define RUNLOOM_THREAD_RET     void *
#define RUNLOOM_THREAD_RETURN(v)  return (v)

/* ============================================================ */
/* condition variable (pairs with runloom_mutex_t)                 */
/* ============================================================ */
/* Used by the blocking-offload pool's worker threads to sleep on an
 * empty job queue, the controlled scheduler's baton, and each M:N hub's
 * idle_cond (the one TIMED waiter).  Callers runloom_cond_init() once; there
 * is no static initializer, so every runloom_cond_t gets the clock below.
 *
 * Timed waits run on the MONOTONIC clock.  A default pthread condvar measures
 * pthread_cond_timedwait's absolute deadline on CLOCK_REALTIME, so on glibc
 * and musl a backward wall-clock step (an NTP step, settimeofday) stretches an
 * in-progress wait by the size of the step.  The hub idle wait
 * backs off to 32 ms (longer under STACKWEAVE_IDLE_BACKOFF_MS) and is also
 * how a hub fires its own sleepers and timed parks, which nothing else
 * signals, so a step would make them fire late by the step.
 *   - Where POSIX clock selection exists (Linux glibc/musl, the BSDs) every
 *     condvar is initialised with pthread_condattr_setclock(CLOCK_MONOTONIC)
 *     and runloom_cond_timedwait_ns computes its deadline on CLOCK_MONOTONIC.
 *     The clock only affects timed waits, so untimed users are unchanged.
 *   - macOS has no pthread_condattr_setclock; it waits with
 *     pthread_cond_timedwait_relative_np, a relative timeout the kernel
 *     measures on mach absolute time, which a wall-clock step does not move.
 *     (Its plain timedwait already converts the deadline to a relative one at
 *     call time, so this only drops the round trip through the wall clock.)
 *   - Anything else keeps the REALTIME deadline.
 * RUNLOOM_COND_MONOTONIC may be predefined to 0 by a checker harness whose
 * pthread model has no condvar attributes (tools/verify/genmc). */
#ifndef RUNLOOM_COND_MONOTONIC
#  if defined(RUNLOOM_OS_MACOS)
#    define RUNLOOM_COND_MONOTONIC 0
#  elif defined(CLOCK_MONOTONIC) && defined(_POSIX_CLOCK_SELECTION) && \
        (_POSIX_CLOCK_SELECTION > 0)
#    define RUNLOOM_COND_MONOTONIC 1
#  else
#    define RUNLOOM_COND_MONOTONIC 0
#  endif
#endif
#if defined(RUNLOOM_OS_MACOS)
/* <pthread.h> declares it only when Darwin extensions are visible (no
 * _POSIX_C_SOURCE / _XOPEN_SOURCE, or _DARWIN_C_SOURCE, which pyconfig.h
 * sets).  Redeclare it so the helper does not depend on include order; a
 * matching redeclaration is harmless where it is already visible. */
int pthread_cond_timedwait_relative_np(pthread_cond_t *, pthread_mutex_t *,
                                       const struct timespec *);
#endif
typedef pthread_cond_t runloom_cond_t;
RUNLOOM_INLINE int  runloom_cond_init(runloom_cond_t *c) {
#if RUNLOOM_COND_MONOTONIC
    pthread_condattr_t attr;
    int rc = pthread_condattr_init(&attr);
    if (rc != 0) return rc;
    rc = pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);
    if (rc == 0) rc = pthread_cond_init(c, &attr);
    pthread_condattr_destroy(&attr);
    return rc;
#else
    return pthread_cond_init(c, NULL);
#endif
}
RUNLOOM_INLINE void runloom_cond_destroy(runloom_cond_t *c) { pthread_cond_destroy(c); }
RUNLOOM_INLINE void runloom_cond_wait(runloom_cond_t *c, runloom_mutex_t *m) {
    pthread_cond_wait(c, m);
}
RUNLOOM_INLINE void runloom_cond_signal(runloom_cond_t *c)    { pthread_cond_signal(c); }
RUNLOOM_INLINE void runloom_cond_broadcast(runloom_cond_t *c) { pthread_cond_broadcast(c); }

/* Timed wait: block on `c` (releasing `m`) until signalled or `rel_ns` elapses.
 * The caller re-evaluates its predicate on return, so the signal-vs-timeout
 * distinction is irrelevant -- no return value, no errno dependency.  Used by
 * the BUG #10 per-hub idle wake: a TIMED wait means a missed signal degrades to
 * the old idle_ns latency, never a hang.  The timeout is measured on a clock a
 * wall-clock step cannot move (see the condvar notes above runloom_cond_init). */
RUNLOOM_INLINE void runloom_cond_timedwait_ns(runloom_cond_t *c, runloom_mutex_t *m,
                                              long long rel_ns) {
    struct timespec ts;
    if (rel_ns < 0) rel_ns = 0;
#if defined(RUNLOOM_OS_MACOS)
    ts.tv_sec  = (time_t)(rel_ns / 1000000000LL);
    ts.tv_nsec = (long)(rel_ns % 1000000000LL);
    pthread_cond_timedwait_relative_np(c, m, &ts);
#else
    {
        long long ns;
        /* The deadline's clock MUST be the one runloom_cond_init gave the
         * condvar: a MONOTONIC deadline read as REALTIME lies in 1970 (every
         * wait returns at once, a busy loop), the reverse is decades out. */
#if RUNLOOM_COND_MONOTONIC
        clock_gettime(CLOCK_MONOTONIC, &ts);
#else
        clock_gettime(CLOCK_REALTIME, &ts);
#endif
        ns = (long long)ts.tv_nsec + rel_ns;
        ts.tv_sec  += (time_t)(ns / 1000000000LL);
        ts.tv_nsec  = (long)(ns % 1000000000LL);
        pthread_cond_timedwait(c, m, &ts);
    }
#endif
}

/* ============================================================ */
/* monotonic clock                                              */
/* ============================================================ */
RUNLOOM_INLINE long long runloom_monotonic_ns(void) {
    struct timespec ts;
#if defined(CLOCK_MONOTONIC)
    if (clock_gettime(CLOCK_MONOTONIC, &ts) == 0) {
        return (long long)ts.tv_sec * 1000000000LL + (long long)ts.tv_nsec;
    }
#endif
    return 0;
}

RUNLOOM_INLINE double runloom_monotonic_seconds_compat(void) {
    return (double)runloom_monotonic_ns() * 1e-9;
}

/* ============================================================ */
/* sleep                                                        */
/* ============================================================ */
RUNLOOM_INLINE void runloom_sleep_ns(long long ns) {
    struct timespec req, rem;
    if (ns <= 0) return;
    req.tv_sec  = (time_t)(ns / 1000000000LL);
    req.tv_nsec = (long)(ns % 1000000000LL);
    /* EINTR -> resume with the remainder; otherwise we'd silently
     * truncate the sleep on signal delivery. */
    while (nanosleep(&req, &rem) == -1 && errno == EINTR) {
        req = rem;
    }
}

/* ============================================================ */
/* CPU count                                                    */
/* ============================================================ */
#if defined(RUNLOOM_OS_BSD) || defined(RUNLOOM_OS_MACOS)
#  include <sys/sysctl.h>
#endif
RUNLOOM_INLINE int runloom_cpu_count(void) {
    long n;
#if defined(_SC_NPROCESSORS_ONLN)
    n = sysconf(_SC_NPROCESSORS_ONLN);
    if (n > 0) return (int)n;
#endif
#if defined(RUNLOOM_OS_BSD) || defined(RUNLOOM_OS_MACOS)
    /* Some BSDs / older macOS lack _SC_NPROCESSORS_ONLN. */
    {
        int mib[2] = { CTL_HW, HW_NCPU };
        int cpu = 0;
        size_t len = sizeof(cpu);
        if (sysctl(mib, 2, &cpu, &len, NULL, 0) == 0 && cpu > 0) {
            return cpu;
        }
    }
#endif
    return 4;
}

#endif /* RUNLOOM_PLAT_COMPAT_H */
