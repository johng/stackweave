"""importlib's module locks know about fibers (stackweave/_importlock.py).

importlib runs a module's code under a per-module lock: the first importer
runs it, any other importer of that module waits for the whole module, and the
importer holding the lock may take it again (a circular import in one thread).
The lock asked the OS thread who was importing, which every fiber on a hub
shares and a migrating fiber changes.  So a second fiber on the importer's
hub got the half-built module, and an importer that moved hubs mid-import
could not release the lock (tests/test_cross_hub_migration.py -k import has
those two).

Each case here runs in its own process:
  * a second importer on the same thread waits, whether the first is parked
    in the module's code or preempted there, under run(1), under run(4) with
    monkey.patch(), and between a fiber and a plain thread either way round;
    a fiber that waits leaves its hub free;
  * plain threads and forked children import as before, and an import in
    flight when stackweave itself is first imported, in a fiber, releases its
    lock as the identity that took it;
  * an import that would wait for an importer that cannot run again until it
    returns (the thread under run(1) or in mn_run(), the hub fiber under a
    nested run(1) or Coro) gets the module as far as it has run, as a
    circular import does, instead of hanging.
"""
import textwrap

import pytest

from adv_util import run_python


PRELUDE = textwrap.dedent("""
    import faulthandler, importlib, os, sys, tempfile, threading, time
    import __main__
    import stackweave, stackweave_c
    faulthandler.dump_traceback_later(45, exit=True)
    DIR = tempfile.mkdtemp()
    sys.path.insert(0, DIR)

    def module(name, body):
        with open(os.path.join(DIR, name + ".py"), "w") as f:
            f.write(body)
        importlib.invalidate_caches()

    def wait_for(cond, secs=10):
        deadline = time.monotonic() + secs
        while not cond():
            if time.monotonic() > deadline:
                raise AssertionError("timed out waiting")
            if stackweave_c.in_fiber():
                stackweave.sleep(0.001)
            else:
                time.sleep(0.001)
""")


def run(code, timeout=60):
    p = run_python(PRELUDE + textwrap.dedent(code), timeout=timeout)
    assert p.returncode == 0 and "PASS" in p.stdout, (
        "rc=%s\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (p.returncode, p.stdout[-3000:], p.stderr[-3000:]))
    return p.stdout


# A module that parks its importer in its body until GATE is sent.
PARKS = """
    module("parks", "import __main__\\n__main__.IN_BODY = True\\n"
                    "__main__.GATE.recv()\\nLATE = 2\\n")
    GATE = stackweave.Chan(0)
    IN_BODY = False
"""


@pytest.mark.parametrize("how", ["run1", "run4_monkey"])
def test_a_second_fiber_on_the_thread_waits_for_a_parked_import(how):
    run(PARKS + """
    HOW = %r
    if HOW == "run4_monkey":
        stackweave.monkey.patch()
    got = {}
    def importer(tag):
        __main__.CALLED.add(tag)
        try:
            got[tag] = importlib.import_module("parks").LATE
        except BaseException as e:
            got[tag] = repr(e)
    CALLED = set()
    def spawn(fn):
        if HOW == "run1":
            stackweave.fiber(fn)
        else:
            stackweave_c.mn_fiber(fn, hub=0)    # both on one OS thread
    def main():
        spawn(lambda: importer("A"))
        wait_for(lambda: IN_BODY)
        spawn(lambda: importer("B"))
        wait_for(lambda: "B" in CALLED)
        stackweave.sleep(0.2)
        assert "B" not in got, ("B did not wait for A's import", got)
        GATE.send(1)
        wait_for(lambda: len(got) == 2)
    stackweave.run(1 if HOW == "run1" else 4, main)
    assert got == {"A": 2, "B": 2}, got
    print("PASS")
    """ % how)


def test_a_fiber_on_the_hub_waits_for_a_preempted_import():
    # A spins in the module's code until B, on the same hub, has called
    # import; B only runs if A is preempted there.
    out = run("""
    module("spins", "import __main__, time\\n__main__.IN_BODY = True\\n"
                    "end = time.monotonic() + 5\\n"
                    "while not __main__.B_CALLED and time.monotonic() < end:\\n"
                    "    pass\\n"
                    "end = time.monotonic() + 0.05\\n"
                    "while time.monotonic() < end:\\n"
                    "    pass\\n"
                    "LATE = 2\\n")
    IN_BODY = B_CALLED = False
    got = {}
    def importer(tag):
        if tag == "B":
            __main__.B_CALLED = True
        try:
            got[tag] = importlib.import_module("spins").LATE
        except BaseException as e:
            got[tag] = repr(e)
    def main():
        stackweave_c.mn_fiber(lambda: importer("A"), hub=0)
        wait_for(lambda: IN_BODY)
        stackweave_c.mn_fiber(lambda: importer("B"), hub=0)
        wait_for(lambda: len(got) == 2)
    stackweave.run(4, main)
    print("PREEMPTED" if B_CALLED else "NOT PREEMPTED", got)
    assert got == {"A": 2, "B": 2}, got
    print("PASS")
    """)
    assert "NOT PREEMPTED" not in out


def test_a_fiber_waiting_for_a_threads_import_leaves_its_thread_free():
    run("""
    module("slow", "import __main__, time\\n__main__.IN_BODY = True\\n"
                   "time.sleep(0.4)\\nLATE = 2\\n")
    IN_BODY = False
    t = threading.Thread(target=lambda: importlib.import_module("slow"))
    ticks = [0]
    seen = {}
    def ticker():
        while "done" not in seen:
            ticks[0] += 1
            stackweave.sleep(0.01)
    def importer():
        start = ticks[0]
        seen["late"] = importlib.import_module("slow").LATE
        seen["ticks"] = ticks[0] - start
        seen["done"] = True
    def main():
        t.start()
        wait_for(lambda: IN_BODY)
        stackweave.fiber(ticker)
        stackweave.fiber(importer)
    stackweave.run(1, main)
    t.join()
    assert seen["late"] == 2, seen
    # On a real lock the waiting fiber blocks run(1)'s thread: no ticks.
    assert seen["ticks"] >= 5, seen
    print("PASS", seen)
    """)


def test_a_thread_waits_for_a_fibers_import():
    run(PARKS + """
    got = {}
    def thread_import():
        __main__.T_CALLED = True
        got["T"] = importlib.import_module("parks").LATE
    T_CALLED = False
    t = threading.Thread(target=thread_import)
    def main():
        def a():
            got["A"] = importlib.import_module("parks").LATE
        stackweave.fiber(a)
        wait_for(lambda: IN_BODY)
        t.start()
        wait_for(lambda: T_CALLED)
        stackweave.sleep(0.2)
        assert "T" not in got, ("the thread did not wait", got)
        GATE.send(1)
        wait_for(lambda: len(got) == 2)
    stackweave.run(4, main)
    t.join()
    assert got == {"A": 2, "T": 2}, got
    print("PASS")
    """)


def test_plain_threads_run_a_module_once():
    run("""
    module("counted", "import __main__, time\\n__main__.RUNS += 1\\n"
                      "time.sleep(0.2)\\nX = 1\\n")
    RUNS = 0
    got = []
    ts = [threading.Thread(target=lambda: got.append(
              importlib.import_module("counted").X)) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert RUNS == 1 and got == [1] * 8, (RUNS, got)
    print("PASS")
    """)


def test_a_child_forked_from_a_fiber_can_import():
    run("""
    import warnings
    warnings.simplefilter("ignore", DeprecationWarning)   # fork with threads
    module("in_child", "Y = 3\\n")
    rcs = []
    def main():
        pid = os.fork()
        if pid == 0:
            import in_child
            os._exit(0 if in_child.Y == 3 else 1)
        rcs.append(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
    stackweave.run(2, main)
    assert rcs == [0], rcs
    print("PASS")
    """)


@pytest.mark.parametrize("hubs", [1, 2])
def test_stackweave_first_imported_in_a_fiber(hubs):
    # The locks of the imports in flight (`outer`, `stackweave`) were taken
    # before stackweave changed importlib's locks; each must still be released
    # as the identity that took it, after `outer` parks.
    p = run_python(textwrap.dedent("""
        import os, sys, tempfile, importlib
        import stackweave_c as rc
        d = tempfile.mkdtemp()
        sys.path.insert(0, d)
        with open(os.path.join(d, "outer.py"), "w") as f:
            f.write("import stackweave\\nstackweave.sleep(0.01)\\nX = 1\\n")
        got = []
        def main():
            try:
                got.append(importlib.import_module("outer").X)
                import stackweave.sync
                got.append(stackweave.sync.WaitGroup is not None)
            except BaseException as e:
                got.append(repr(e))
        HUBS = %d
        if HUBS == 1:
            rc.fiber(main); rc.run()
        else:
            rc.mn_init(HUBS); rc.mn_fiber(main); rc.mn_run(); rc.mn_fini()
        assert "stackweave" in sys.modules
        assert got == [1, True], got
        print("PASS")
    """ % hubs), timeout=60)
    assert p.returncode == 0 and "PASS" in p.stdout, (p.stdout[-2000:], p.stderr[-3000:])


# The importer of `runs` runs fibers in the module's code (how=...), and they
# import `runs` too.  Each would wait for an importer that cannot run again
# until it returns.
RUNS_IN_BODY = {
    "run1": ("stackweave.run(1, f)\n", "importlib.import_module('runs')"),
    "run4": ("stackweave.run(4, f)\n", "importlib.import_module('runs')"),
    "nested_run1": ("stackweave.run(1, f)\n",
                    "stackweave.run(4, lambda: importlib.import_module('runs'))"),
    "coro": ("stackweave_c.Coro(f).resume()\n",
             "stackweave.run(4, lambda: importlib.import_module('runs'))"),
}


@pytest.mark.parametrize("how", list(RUNS_IN_BODY))
def test_an_import_its_importer_waits_on_gets_the_module_so_far(how):
    in_body, outer = RUNS_IN_BODY[how]
    run("""
    module("runs", "import importlib, stackweave, stackweave_c, __main__\\n"
                   "def f():\\n"
                   "    import runs\\n"
                   "    __main__.GOT.append(hasattr(runs, 'DONE'))\\n"
                   "    try:\\n"
                   "        importlib.import_module('runs')\\n"
                   "    except Exception as e:\\n"
                   "        __main__.GOT.append(type(e).__name__)\\n"
                   %r
                   "DONE = True\\n")
    GOT = []
    %s
    # `import runs` gets the module as far as it had run, as a circular import
    # does; import_module raises importlib's deadlock error, as between threads.
    assert GOT == [False, "_DeadlockError"], GOT
    assert sys.modules["runs"].DONE
    print("PASS")
    """ % (in_body, outer))
