"""Cooperative waits for a locked sqlite3 database.

sqlite waits for a lock held by another connection in its busy handler,
inside sqlite3_step on the calling OS thread: the default one, installed by
``sqlite3.connect(timeout=...)``, sleeps there until the timeout.  On a hub
that blocks every fiber of the hub -- including a fiber that holds the write
lock across a park, so the waiter can only time out
(docs/dev/FT_CPYTHON_LOCK_GAPS.md #7).  Parking inside the handler is not an
option: the statement is mid-flight there, and another fiber of the same hub
could reach the same connection (sqlite's busy handler is not reentrant, and
serialized mode's recursive mutex lets a second caller on the same OS thread
in).

So the waiting moves out of sqlite.  ``connect()`` (patched here) opens the
connection as before, then replaces its busy handler with stackweave's
(``stackweave_c._sqlite_busy_handler``, see module_sqlite.c.inc).  Around the
calls sqlite says may be retried after SQLITE_BUSY -- a statement
(``Cursor.execute``, ``Connection.execute``) run while the connection is in
autocommit, and a commit (``commit()``, ``with connection:``, a COMMIT
statement) -- a fiber arms the handler for that connection; when sqlite asks
it to wait, it gives up at once, sqlite resets the statement and returns
SQLITE_BUSY, and the call sleeps cooperatively on sqlite's own backoff (1, 2,
5, 10, ... 100 ms, up to the connection's timeout) and runs again, after
rolling back the transaction Python's implicit BEGIN opened for it, if any.
A BUSY sqlite returns without consulting the handler -- the deadlock it breaks
by failing a reader that wants to write -- is raised at once, as before.  An
armed statement whose cache spill asks for the exclusive lock gets no wait for
it: the spill fails and sqlite grows the page cache instead.  Everything else
keeps sqlite's behaviour, the same backoff slept on the OS thread, which
blocks the hub as any blocking C call does: a statement inside an open
transaction (retrying it there is not sound), a call made while a foreign C
caller is on the stack (a statement run from a user function of another
connection: the outer statement is suspended, so nothing may park),
``executemany``, ``executescript``, fetches, ``backup``, blobs, and a call off
a fiber.

What the patch changes for the caller: ``connect()`` returns an instance of a
subclass of the requested factory (same name), whose ``cursor()`` returns a
subclass of the requested cursor class.  ``PRAGMA busy_timeout`` reinstalls
sqlite's default handler on that connection.  Connections made before
``patch()``, or by calling ``sqlite3.Connection`` directly, are untouched.

How the connection's ``sqlite3 *`` is found: Python does not expose it, so
it is read from the Connection object, whose first field after the object
header it is (``pysqlite_Connection.db``, Modules/_sqlite/connection.h, the
same in 3.14 and 3.15).  The patch is applied only on CPython versions
whose layout was checked (_LAYOUT_CHECKED), and each handle is cross-checked
against what the connection reports (autocommit, total changes) before the
handler goes in.  sqlite3_auto_extension, the documented way to see each new
handle, returns SQLITE_MISUSE in macOS's system library.  The C call goes
through the library ``_sqlite3`` itself is linked to (resolved through its
own handle).  Where any of this is not possible -- another CPython version,
sqlite linked in statically without exported symbols, a 32-bit build -- the
patch does nothing.
"""
import itertools
import sys

import stackweave_c

from ._base import _co_sleep

_SQLITE_BUSY = 5
# sqliteDefaultBusyCallback's schedule (sqlite3/src/main.c), also in C.
_DELAYS = (1, 2, 5, 10, 15, 20, 25, 25, 25, 50, 50, 100)
_TOTALS = (0, 1, 3, 8, 18, 33, 53, 78, 103, 128, 178, 228)

_coop = getattr(stackweave_c, "_sqlite_coop", None)   # None: a stale build
_orig_connect = None
_state = None          # a _State, built on the first patch()
# Cooperative busy waits taken (an odometer for tests; a lost increment under
# free threading is harmless).
cooperative_waits = 0
_conn_classes = {}
_cursor_classes = {}
_ids = itertools.count(1)


def _busy_delay(count, timeout_ms):
    """ms to sleep before retry `count` (0-based), or <= 0 to give up."""
    if count < len(_DELAYS):
        delay, prior = _DELAYS[count], _TOTALS[count]
    else:
        delay = _DELAYS[-1]
        prior = _TOTALS[-1] + delay * (count - (len(_DELAYS) - 1))
    if prior + delay > timeout_ms:
        delay = timeout_ms - prior
    return delay


def _is_commit(sql):
    """Whether `sql` is a COMMIT (or END) statement, which sqlite says may be
    retried after SQLITE_BUSY even inside a transaction."""
    try:
        head = sql.lstrip()[:6].upper()
    except AttributeError:
        return False
    return head == "COMMIT" or head[:3] == "END"


def _retry(con, from_autocommit, call, *args):
    """call(*args) with con's busy waits armed: when sqlite's busy handler
    gives one back (bit 0 of the arm word), sleep cooperatively on sqlite's
    backoff and call again, until con's timeout.

    Only for a call sqlite says may be retried after SQLITE_BUSY: a statement
    run while the connection is in autocommit (from_autocommit), or a commit.
    Off a fiber, or with a foreign C caller on the stack (a statement run from
    a user function of another connection, which must not park mid-statement),
    the handler is not armed and sqlite waits as it always does."""
    global cooperative_waits
    arm = con._sw_id << 1
    prev = _coop(arm)
    if prev < 0:
        return call(*args)
    try:
        count = 0
        while True:
            try:
                return call(*args)
            except _state.OperationalError as exc:
                word = _coop(arm)       # read it, and re-arm for the next try
                if (word < 0 or not word & 1 or
                        (exc.sqlite_errorcode & 0xFF) != _SQLITE_BUSY):
                    raise
                delay = _busy_delay(count, con._sw_ms)
                if delay <= 0:
                    raise
                if from_autocommit and con.in_transaction:
                    # The call opened a transaction (Python's implicit BEGIN
                    # before a DML statement) and its statement failed in it:
                    # leave the connection as the call found it.
                    con.rollback()
            cooperative_waits += 1
            if con._sw_hold:
                # sqlite's own wait never left the OS thread, and this
                # connection checks it: sleep on this hub.
                stackweave_c._hold_hub(1)
                try:
                    _co_sleep(delay / 1000.0)
                finally:
                    stackweave_c._hold_hub(-1)
            else:
                _co_sleep(delay / 1000.0)
            count += 1
    finally:
        _coop(prev)


class _CoopCursor:
    """Mixed in ahead of the cursor class a patched connection hands out."""

    def execute(self, sql, parameters=(), /):
        con = self.connection
        if getattr(con, "_sw_ms", 0):
            if not con.in_transaction:
                return _retry(con, True, super().execute, sql, parameters)
            if _is_commit(sql):
                return _retry(con, False, super().execute, sql, parameters)
        return super().execute(sql, parameters)


class _CoopConnection:
    """Mixed in ahead of the factory a patched connect() was given."""

    _sw_id = 0
    _sw_ms = 0
    _sw_hold = True     # check_same_thread: stay on the OS thread

    def cursor(self, factory=None):
        if factory is None:
            return super().cursor(_state.coop_cursor)
        return super().cursor(_cursor_class(factory))

    def execute(self, sql, parameters=(), /):
        if not self._sw_ms:
            return super().execute(sql, parameters)
        # As Connection.execute: a plain cursor from Connection.cursor (an
        # override of cursor() is not consulted), and Cursor.execute on it.
        st = _state
        cur = st.conn_cursor(self, st.coop_cursor)
        if not self.in_transaction:
            _retry(self, True, st.cursor_execute, cur, sql, parameters)
        elif _is_commit(sql):
            _retry(self, False, st.cursor_execute, cur, sql, parameters)
        else:
            st.cursor_execute(cur, sql, parameters)
        return cur

    def commit(self):
        if self._sw_ms:
            return _retry(self, False, super().commit)
        return super().commit()

    def __exit__(self, exc_type, exc_value, traceback, /):
        if not self._sw_ms:
            return super().__exit__(exc_type, exc_value, traceback)
        # Connection.__exit__, with the commit retried: commit on a clean
        # exit, and roll back if that fails (a rollback that fails too is
        # raised with the commit's error as its context); roll back otherwise.
        if exc_type is None and exc_value is None and traceback is None:
            try:
                self.commit()
            except BaseException:
                self.rollback()
                raise
        else:
            self.rollback()
        return False


def _subclass(cache, mixin, base):
    cls = cache.get(base)
    if cls is None:
        cls = type(base.__name__, (mixin, base),
                   {"__module__": base.__module__,
                    "__qualname__": base.__qualname__})
        cls = cache.setdefault(base, cls)
    return cls


def _cursor_class(factory):
    if isinstance(factory, type) and issubclass(factory, _CoopCursor):
        return factory
    if not (isinstance(factory, type) and issubclass(factory, _state.Cursor)):
        return factory          # let Connection.cursor() reject it
    return _subclass(_cursor_classes, _CoopCursor, factory)


# CPython versions whose pysqlite_Connection starts with `sqlite3 *db`.
_LAYOUT_CHECKED = ((3, 14), (3, 15))


class _State:
    """The ctypes side, built on the first patch()."""

    def __init__(self):
        import ctypes
        import importlib.util
        import sqlite3
        if sys.version_info[:2] not in _LAYOUT_CHECKED:
            raise OSError("pysqlite_Connection layout not checked for %d.%d"
                          % sys.version_info[:2])
        self.Cursor = sqlite3.Cursor
        self.Connection = sqlite3.Connection
        self.OperationalError = sqlite3.OperationalError
        self.conn_cursor = sqlite3.Connection.cursor
        self.cursor_execute = sqlite3.Cursor.execute
        self.coop_cursor = _subclass(_cursor_classes, _CoopCursor,
                                     sqlite3.Cursor)
        self.handler = stackweave_c._sqlite_busy_handler()
        if not self.handler or _coop is None:
            raise OSError("no 64-bit busy-handler argument on this build")
        self.db_offset = object.__basicsize__
        self.c_void_p = ctypes.c_void_p
        lib = ctypes.CDLL(importlib.util.find_spec("_sqlite3").origin)
        self.busy_handler = lib.sqlite3_busy_handler
        self.busy_handler.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p]
        self.busy_handler.restype = ctypes.c_int
        self.get_autocommit = lib.sqlite3_get_autocommit
        self.get_autocommit.argtypes = [ctypes.c_void_p]
        self.get_autocommit.restype = ctypes.c_int
        self.total_changes = lib.sqlite3_total_changes
        self.total_changes.argtypes = [ctypes.c_void_p]
        self.total_changes.restype = ctypes.c_int

    def db_handle(self, con):
        """con's sqlite3 *, or None if it does not look like one."""
        db = self.c_void_p.from_address(id(con) + self.db_offset).value
        if not db or db % 8:
            return None
        if (bool(self.get_autocommit(db)) == con.in_transaction or
                self.total_changes(db) != con.total_changes):
            return None
        return db


def _patched_connect(database, *args, **kwargs):
    st = _state
    if len(args) > 4:
        factory = args[4]
    else:
        factory = kwargs.get("factory", st.Connection)
    timeout = args[0] if args else kwargs.get("timeout", 5.0)
    if len(args) > 3:
        same_thread = args[3]
    else:
        same_thread = kwargs.get("check_same_thread", True)
    if not (isinstance(factory, type) and issubclass(factory, st.Connection)):
        return _orig_connect(database, *args, **kwargs)
    cls = _subclass(_conn_classes, _CoopConnection, factory)
    if len(args) > 4:
        args = args[:4] + (cls,) + args[5:]
    else:
        kwargs["factory"] = cls
    con = _orig_connect(database, *args, **kwargs)
    try:
        timeout_ms = min(int(timeout * 1000), 0x7FFFFFFF)
    except (TypeError, ValueError, OverflowError):
        timeout_ms = 0
    if timeout_ms > 0 and isinstance(con, cls):
        db = st.db_handle(con)
        cid = next(_ids) & 0xFFFFFFFF or 1
        if db and st.busy_handler(db, st.handler,
                                  (cid << 32) | timeout_ms) == 0:
            con._sw_id = cid
            con._sw_ms = timeout_ms
            con._sw_hold = bool(same_thread)
    return con


def _patch_sqlite():
    global _orig_connect, _state
    if _orig_connect is not None:
        return
    try:
        import sqlite3
        import sqlite3.dbapi2
        if _state is None:
            _state = _State()
    except (ImportError, OSError, AttributeError):
        return                          # no sqlite3, or no way to reach it
    _orig_connect = sqlite3.connect
    sqlite3.connect = _patched_connect
    sqlite3.dbapi2.connect = _patched_connect


def _unpatch_sqlite():
    global _orig_connect
    if _orig_connect is None:
        return
    sqlite3 = sys.modules.get("sqlite3")
    if sqlite3 is not None:
        sqlite3.connect = _orig_connect
        sqlite3.dbapi2.connect = _orig_connect
    _orig_connect = None
