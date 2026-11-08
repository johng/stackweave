"""monkey.patch(sqlite=True): a fiber waits for a locked database cooperatively.

sqlite's own busy handler sleeps inside sqlite3_step on the hub's OS thread;
the patch (src/stackweave/monkey/sqlite.py, module_sqlite.c.inc) has the
handler give up for the calls sqlite says may be retried as a whole -- a
statement run in autocommit, a commit -- and sleeps cooperatively on
sqlite's backoff instead.  These scenarios pin down what must not change
while it does: a BUSY that sqlite raises without waiting still raises at
once, a commit waits for a reader on its own hub, a thread-checked
connection keeps its OS thread, a statement inside a transaction and one run
from a user function keep sqlite's own wait, and connect() keeps the factory
and cursor types it was given.  The same-hub writer case is
tests/test_cpython_thread_keyed_locks.py::
test_preempt_sqlite_writer_waits_for_a_same_hub_transaction.

Each scenario runs in a fresh interpreter (the patch is process-wide).
"""
from test_cross_hub_migration import assert_pass

SETUP = r'''
import stackweave.monkey as monkey; monkey.patch()
import sqlite3, tempfile, time as _t
from stackweave.monkey import sqlite as sw_sqlite
_watchdog(30)
path = tempfile.mktemp(suffix=".db")
c0 = sqlite3.connect(path); c0.execute("create table t(x)")
c0.execute("insert into t values (0)"); c0.commit(); c0.close()
'''


def test_deadlock_busy_is_raised_without_waiting():
    """A reader that wants to write while another connection holds the
    reserved lock is failed at once by sqlite (waiting would deadlock), not
    handed to the busy handler.  The patch must not turn that into a wait:
    the insert raises SQLITE_BUSY well before the 2 s timeout and no
    cooperative wait is taken."""
    assert_pass(SETUP + r'''
def main():
    a = sqlite3.connect(path, timeout=2, isolation_level=None)
    b = sqlite3.connect(path, timeout=2, isolation_level=None)
    a.execute("begin"); a.execute("select * from t").fetchall()   # shared lock
    b.execute("begin immediate")                                   # reserved lock
    waits = sw_sqlite.cooperative_waits
    t0 = _t.monotonic()
    try:
        a.execute("insert into t values (1)")
        err = None
    except sqlite3.OperationalError as e:
        err = e
    took = _t.monotonic() - t0
    a.rollback(); b.rollback()
    print("err=%r code=%s took=%.3f waits=%d"
          % (err, err and err.sqlite_errorcode, took,
             sw_sqlite.cooperative_waits - waits), flush=True)
    assert err is not None and err.sqlite_errorcode & 0xFF == 5, err
    assert took < 0.5, took
    assert sw_sqlite.cooperative_waits == waits
    print("PASS", flush=True)
stackweave.run(2, main)
''')


def test_commit_waits_for_a_reader_on_the_same_hub():
    """A commit needs the exclusive lock, which a reader's shared lock
    blocks.  The reader runs on the same hub and finishes its transaction
    after a park; the writer's ``with connection:`` commit must wait for it
    cooperatively and succeed, not sleep the hub through its timeout."""
    assert_pass(SETUP + r'''
def main():
    out = {}
    wg = stackweave.WaitGroup(); wg.add(2)
    reading = []
    def reader():
        r = sqlite3.connect(path, timeout=2, isolation_level=None)
        r.execute("begin"); r.execute("select * from t").fetchall()
        reading.append(1)
        stackweave.sleep(0.05)
        r.execute("commit"); reading.pop(); r.close(); wg.done()
    def writer():
        w = sqlite3.connect(path, timeout=2)
        assert reading, "the reader was not holding its shared lock"
        t0 = _t.monotonic()
        try:
            with w:
                w.execute("insert into t values (2)")
            out["w"] = "ok"
        except Exception as e:
            out["w"] = repr(e)
        out["waited"] = round(_t.monotonic() - t0, 3); w.close(); wg.done()
    waits = sw_sqlite.cooperative_waits
    stackweave_c.mn_fiber(reader, hub=0)
    stackweave.sleep(0.01)
    stackweave_c.mn_fiber(writer, hub=0)
    wg.wait()
    out["waits"] = sw_sqlite.cooperative_waits - waits
    rows = sqlite3.connect(path).execute("select x from t order by x").fetchall()
    print(out, rows, flush=True)
    assert out["w"] == "ok" and out["waited"] < 1.0, out
    assert out["waits"] > 0, out
    assert rows == [(0,), (2,)], rows
    print("PASS", flush=True)
stackweave.run(2, main)
''')


def test_connect_keeps_the_factory_and_cursor_types():
    """connect() hands back a subclass of the factory it was given, under the
    same name, whose cursor() honours a cursor factory; a connection used off
    any fiber keeps sqlite's own behaviour; unpatch() restores connect."""
    assert_pass(SETUP + r'''
class MyConn(sqlite3.Connection):
    pass
class MyCur(sqlite3.Cursor):
    pass
con = sqlite3.connect(":memory:", factory=MyConn, check_same_thread=False)
assert isinstance(con, MyConn) and type(con).__name__ == "MyConn", type(con)
assert con._sw_ms == 5000, con._sw_ms
assert isinstance(con.cursor(MyCur), MyCur)
assert isinstance(con.cursor(), sqlite3.Cursor)
assert con.execute("select 41 + 1").fetchone() == (42,)       # off a fiber
with con:
    con.execute("create table u(x)")
plain = sqlite3.connect(":memory:", timeout=0)
assert plain._sw_ms == 0                                      # no wait asked for
seen = []
def main():
    seen.append(con.execute("select count(*) from u").fetchone())
stackweave.run(2, main)
assert seen == [(0,)], seen
monkey.unpatch(sqlite=True)
assert type(sqlite3.connect(":memory:")) is sqlite3.Connection
print("PASS", flush=True)
''')


def test_thread_checked_connection_waits_on_its_os_thread():
    """sqlite's own busy wait never left the OS thread, and a connection made
    with the default check_same_thread=True raises ProgrammingError on any
    other.  So a fiber sleeping out a busy wait on such a connection must
    resume on the OS thread it slept on, wherever its timer would let it go.
    Sixteen unpinned writers on four hubs wait for a holder that keeps the
    write lock for 150 ms; every one must commit, from the thread it began
    on."""
    assert_pass(SETUP + r'''
def main():
    out = {"ok": 0, "err": [], "moved": 0}
    holding = stackweave.Chan(1)
    wg = stackweave.WaitGroup(); wg.add(17)
    def holder():
        a = sqlite3.connect(path, timeout=5)
        a.execute("insert into t values (1)")
        holding.send(1)
        stackweave.sleep(0.15)
        a.commit(); a.close(); wg.done()
    def writer(i):
        try:
            b = sqlite3.connect(path, timeout=5)
            tid = threading.get_ident()
            b.execute("insert into t values (?)", (100 + i,))
            b.commit()
            if threading.get_ident() != tid:
                out["moved"] += 1
            b.close(); out["ok"] += 1
        except Exception as e:
            out["err"].append(repr(e))
        finally:
            wg.done()
    waits = sw_sqlite.cooperative_waits
    stackweave.fiber(holder)
    holding.recv()
    for i in range(16):
        stackweave.fiber(lambda i=i: writer(i))
    wg.wait()
    out["waits"] = sw_sqlite.cooperative_waits - waits
    n = sqlite3.connect(path).execute("select count(*) from t").fetchone()[0]
    print(out, "rows", n, flush=True)
    assert out["err"] == [] and out["ok"] == 16 and out["moved"] == 0, out
    assert out["waits"] > 0, out
    assert n == 18, n
    print("PASS", flush=True)
stackweave.run(4, main)
''')


def test_statement_in_a_transaction_keeps_sqlites_wait():
    """sqlite says a statement that got SQLITE_BUSY inside an open
    transaction may not simply be retried, so the patch leaves it sqlite's
    own wait.  Provoked here by a cache spill: with a 2-page cache a long
    transaction asks for the exclusive lock in the middle of an insert, which
    a reader's shared lock refuses.  That insert must wait the connection's
    0.2 s out (then sqlite grows the cache and goes on), not be handed back,
    and no cooperative wait is taken; every row lands once."""
    assert_pass(SETUP + r'''
def main():
    r = sqlite3.connect(path, timeout=0.2, isolation_level=None)
    r.execute("begin"); r.execute("select * from t").fetchall()   # shared lock
    w = sqlite3.connect(path, timeout=0.2)
    w.execute("pragma cache_size=2"); w.execute("pragma cache_spill=1")
    waits = sw_sqlite.cooperative_waits
    slow = None
    for i in range(400):
        t0 = _t.monotonic()
        w.execute("insert into t values (?)", (b"x" * 3000,))
        if _t.monotonic() - t0 > 0.15:
            slow = (i, w.in_transaction)
            break
    waits = sw_sqlite.cooperative_waits - waits
    r.execute("commit")
    w.commit()
    n = w.execute("select count(*) from t").fetchone()[0]
    print("slow insert (index, in transaction)=%s waits=%d rows=%d"
          % (slow, waits, n), flush=True)
    assert slow is not None and slow[1], "no insert waited for the spill's lock"
    assert waits == 0, waits
    assert n == slow[0] + 2, n
    print("PASS", flush=True)
stackweave.run(2, main)
''')


def test_statement_run_from_a_user_function_does_not_park():
    """A user function of one connection that runs SQL on another runs inside
    the first one's sqlite3_step, with a foreign C caller (sqlite) on the
    stack.  A busy wait there must not park: another fiber of the hub could
    then enter the outer connection mid-statement.  So the inner insert,
    blocked by a reserved lock that is never released, waits sqlite's own
    0.2 s on the OS thread and raises SQLITE_BUSY; a sibling fiber on the same
    hub makes no progress meanwhile, and no cooperative wait is taken."""
    assert_pass(SETUP + r'''
path2 = tempfile.mktemp(suffix=".db")
c2 = sqlite3.connect(path2); c2.execute("create table u(x)"); c2.commit(); c2.close()
def main():
    holder = sqlite3.connect(path2, isolation_level=None, check_same_thread=False)
    holder.execute("begin immediate")                              # reserved lock
    res = {}
    ticks = [0]
    done = []
    wg = stackweave.WaitGroup(); wg.add(2)
    def sibling():
        while not done:
            stackweave.sleep(0.005)
            ticks[0] += 1
        wg.done()
    def runner():
        outer = sqlite3.connect(":memory:")
        inner = sqlite3.connect(path2, timeout=0.2)
        def udf(x):
            seen, t0 = ticks[0], _t.monotonic()
            try:
                inner.execute("insert into u values (1)")
                res["inner"] = "ok"
            except sqlite3.OperationalError as e:
                res["inner"] = e.sqlite_errorcode & 0xFF
            res["waited"] = round(_t.monotonic() - t0, 3)
            res["sibling_ticks"] = ticks[0] - seen
            return x
        outer.create_function("f", 1, udf)
        res["outer"] = outer.execute("select f(7)").fetchone()
        done.append(1)
        wg.done()
    waits = sw_sqlite.cooperative_waits
    stackweave_c.mn_fiber(sibling, hub=0)
    stackweave.sleep(0.02)
    stackweave_c.mn_fiber(runner, hub=0)
    wg.wait()
    res["waits"] = sw_sqlite.cooperative_waits - waits
    holder.rollback()
    print(res, flush=True)
    assert res["outer"] == (7,) and res["inner"] == 5, res
    assert res["waited"] >= 0.15 and res["sibling_ticks"] == 0, res
    assert res["waits"] == 0, res
    print("PASS", flush=True)
stackweave.run(2, main)
''')
