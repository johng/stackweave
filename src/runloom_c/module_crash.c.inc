/* Part of module.c -- #included into that translation unit, not compiled
 * standalone.  Python bindings for the fatal-signal crash reporter
 * (runloom_crash.c). */

static PyObject *m_install_crash_handler(PyObject *self, PyObject *args,
                                         PyObject *kw)
{
    static char *kwlist[] = {"level", "file", NULL};
    const char *level = NULL;
    const char *path  = NULL;
    int flags;
    (void)self;
    if (!PyArg_ParseTupleAndKeywords(args, kw, "|zz", kwlist, &level, &path))
        return NULL;

    flags = (level != NULL && level[0] != '\0') ? runloom_crash_parse_flags(level)
                                                : RUNLOOM_CRASH_DEFAULT;

    if (flags < 0) {            /* "off" -- treat as uninstall */
        runloom_crash_uninstall();
        Py_RETURN_NONE;
    }
    if (runloom_crash_install(flags, path) != 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }
    return PyLong_FromLong((long)flags);
}

static PyObject *m_uninstall_crash_handler(PyObject *self, PyObject *unused)
{
    (void)self; (void)unused;
    runloom_crash_uninstall();
    Py_RETURN_NONE;
}

static PyObject *m_crash_handler_installed(PyObject *self, PyObject *unused)
{
    (void)self; (void)unused;
    return PyBool_FromLong(runloom_crash_installed());
}

static PyObject *m_start_watchdog(PyObject *self, PyObject *arg)
{
    long secs = PyLong_AsLong(arg);
    (void)self;
    if (secs == -1 && PyErr_Occurred()) return NULL;
    if (secs <= 0 || secs > INT_MAX) {
        PyErr_SetString(PyExc_ValueError,
                        "start_watchdog: secs must be a positive int");
        return NULL;
    }
    if (runloom_watchdog_start((int)secs) != 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *m_crash_thread_arm(PyObject *self, PyObject *unused)
{
    (void)self; (void)unused;
    runloom_crash_thread_arm();
    Py_RETURN_NONE;
}

/* Test-only: deliberately overflow the current C stack (does not return).
 * Call inside a fiber to fault into its guard page. */
static PyObject *m_crash_selftest_overflow(PyObject *self, PyObject *unused)
{
    (void)self; (void)unused;
    Py_BEGIN_ALLOW_THREADS
    runloom_crash_selftest_overflow();
    Py_END_ALLOW_THREADS
    Py_RETURN_NONE;   /* unreachable */
}
