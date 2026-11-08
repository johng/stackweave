/* module_sqlite.c.inc -- the sqlite3 busy handler behind monkey.patch(sqlite=True).
 *
 * sqlite waits for a locked database in its busy handler, which runs inside
 * sqlite3_step (or prepare) on the calling OS thread with the connection
 * mutex held: the default one, installed by Connection(timeout=), sleeps there
 * with a fixed backoff until the timeout.  On a hub thread that sleep blocks
 * every fiber of the hub, including a fiber of the same hub that holds the
 * write lock across a park and is the only thing that could end the wait.
 * Parking inside the handler instead would let another fiber of the hub reach
 * the same connection while sqlite is mid-statement, which sqlite does not
 * allow (its busy handler is not reentrant; serialized mode's recursive mutex
 * would let the second fiber in).
 *
 * So the wait moves out of sqlite3_step.  monkey/sqlite.py installs this
 * handler with sqlite3_busy_handler on each connection it opens, with
 * arg = (connection id << 32) | timeout in ms, and arms the running fiber
 * (g->sqlite_coop = id << 1) around the calls it can retry as a whole: a
 * statement run while the connection is in autocommit, and a commit (sqlite's
 * guidance for SQLITE_BUSY), never one inside an open transaction, and never
 * while a foreign C caller is on the stack (a statement run from inside
 * another connection's user function must not park).  Invoked for an armed
 * connection, the handler marks the fiber (bit 0) and gives up at once:
 * sqlite resets the statement and returns SQLITE_BUSY, and the wrapper sleeps
 * cooperatively on the same backoff and calls again (held on its hub when the
 * connection checks its OS thread: m_hold_hub below).  A BUSY sqlite returns
 * without consulting the handler -- the deadlock it breaks by failing a
 * reader that wants to write -- reaches the caller unchanged.  One request an
 * armed statement can make is not its own lock: the exclusive lock a cache
 * spill asks for in the middle of a big statement.  Handed back, the spill
 * fails and sqlite lets the page cache grow instead, so the statement goes on
 * without that wait.  Anything else (no fiber, a call that is not armed, a
 * statement in an open transaction, executemany, executescript, a fetch) gets
 * sqlite's own default behaviour: the same backoff, slept here, blocking the
 * thread. */

#include <errno.h>
#include <stdint.h>
#include <time.h>

#if INTPTR_MAX > INT32_MAX
#  define RUNLOOM_SQLITE_BUSY_OK 1
#else
#  define RUNLOOM_SQLITE_BUSY_OK 0   /* arg cannot carry id + timeout */
#endif

static runloom_g_t *runloom_sqlite_current_g(void)
{
    if (runloom_mn_current_hub_opaque() != NULL)
        return runloom_mn_tls_current_g();
    return runloom_sched_peek_current();
}

/* Whether a foreign C caller -- a library that called back into Python through
 * PyGILState_Ensure, e.g. sqlite running a user function -- is on this
 * thread's current stack: its thread state's gilstate_counter is above the 1
 * it was created with.  A wait there must not park (the outer library may be
 * mid-statement), so it is never armed and never handed back.  The gilstate
 * slot names the fiber's state whether or not it is attached right now (the
 * handler runs with it detached, inside sqlite3_step). */
static int runloom_sqlite_in_c_caller(void)
{
    PyThreadState *ts = PyGILState_GetThisThreadState();
    return ts != NULL && ts->gilstate_counter > 1;
}

/* sqliteDefaultBusyCallback's schedule (sqlite3/src/main.c). */
static const unsigned char runloom_sqlite_delays[] =
    { 1, 2, 5, 10, 15, 20, 25, 25, 25, 50, 50, 100 };
static const unsigned char runloom_sqlite_totals[] =
    { 0, 1, 3, 8, 18, 33, 53, 78, 103, 128, 178, 228 };
#define RUNLOOM_SQLITE_NDELAY \
    ((int)(sizeof(runloom_sqlite_delays) / sizeof(runloom_sqlite_delays[0])))

static int runloom_sqlite_busy(void *arg, int count)
{
    uint64_t a = (uint64_t)(uintptr_t)arg;
    uint32_t id = (uint32_t)(a >> 32);
    int tmout = (int)(a & 0xffffffffu);
    int delay, prior;
    runloom_g_t *g = runloom_sqlite_current_g();
    if (g != NULL && id != 0 && (g->sqlite_coop >> 1) == (uint64_t)id &&
        !runloom_sqlite_in_c_caller()) {
        g->sqlite_coop |= 1;               /* the caller retries cooperatively */
        return 0;
    }
    if (count < RUNLOOM_SQLITE_NDELAY) {
        delay = runloom_sqlite_delays[count];
        prior = runloom_sqlite_totals[count];
    } else {
        delay = runloom_sqlite_delays[RUNLOOM_SQLITE_NDELAY - 1];
        prior = runloom_sqlite_totals[RUNLOOM_SQLITE_NDELAY - 1] +
                delay * (count - (RUNLOOM_SQLITE_NDELAY - 1));
    }
    if (prior + delay > tmout) {
        delay = tmout - prior;
        if (delay <= 0) return 0;
    }
    {
        struct timespec ts;
        ts.tv_sec = delay / 1000;
        ts.tv_nsec = (long)(delay % 1000) * 1000000L;
        while (nanosleep(&ts, &ts) != 0 && errno == EINTR) { }
    }
    return 1;
}

/* _sqlite_busy_handler() -> int: the handler's address, for
 * sqlite3_busy_handler through ctypes; 0 where arg cannot carry it. */
static PyObject *m_sqlite_busy_handler(PyObject *self, PyObject *unused)
{
    (void)self; (void)unused;
    if (!RUNLOOM_SQLITE_BUSY_OK) return PyLong_FromLong(0);
    return PyLong_FromVoidPtr((void *)(uintptr_t)&runloom_sqlite_busy);
}

/* _sqlite_coop(value) -> int: set the running fiber's sqlite arm word to
 * `value` (connection id << 1, or 0) and return the old word, whose bit 0 says
 * the handler gave up for it.  -1, changing nothing, when no fiber runs here,
 * or when `value` would arm while a foreign C caller is on the stack. */
static PyObject *m_sqlite_coop(PyObject *self, PyObject *arg)
{
    unsigned long long v;
    uint64_t old;
    runloom_g_t *g;
    (void)self;
    v = PyLong_AsUnsignedLongLong(arg);
    if (v == (unsigned long long)-1 && PyErr_Occurred()) return NULL;
    g = runloom_sqlite_current_g();
    if (g == NULL || (v != 0 && runloom_sqlite_in_c_caller()))
        return PyLong_FromLong(-1);
    old = g->sqlite_coop;
    g->sqlite_coop = (uint64_t)v;
    return PyLong_FromUnsignedLongLong((unsigned long long)old);
}

/* _hold_hub(delta) -> int: runloom_mn_hold_here.  monkey/sqlite.py holds a
 * fiber whose connection checks its OS thread on its hub while it sleeps out
 * a busy wait, which sqlite's own wait never left. */
static PyObject *m_hold_hub(PyObject *self, PyObject *arg)
{
    long d;
    (void)self;
    d = PyLong_AsLong(arg);
    if (d == -1 && PyErr_Occurred()) return NULL;
    return PyLong_FromLong(runloom_mn_hold_here((int)d));
}
