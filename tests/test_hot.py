"""@stackweave.hot per-core handler scaling.

The contention these fix is SHARED CLOSURE CELLS: one closure (e.g.
``handler = make_app(config)``) run by many fibers across many cores makes the
cores fight over the captured slots.  @hot gives each core its own cells holding
the same values -- distinct cells, SHARED code (the code was never the problem).
A module-level def captures nothing and already scales, so @hot is a no-op there.
Runnable standalone or under pytest.
"""
import threading

import stackweave


def test_hot_splits_cells_per_thread_and_shares_code():
    captured = {"n": 7}                       # a read-only capture

    @stackweave.hot
    def work(x):
        return x * captured["n"]              # reads the captured dict

    N = 8
    results = {}
    barrier = threading.Barrier(N)

    def run_one(i):
        barrier.wait()                        # maximise real concurrency
        results[i] = work(i)

    threads = [threading.Thread(target=run_one, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(results[i] == i * 7 for i in range(N)), results        # correct
    copies = work._runloom_copies
    assert len(copies) == N, copies                                   # one per thread
    fns = list(copies.values())
    assert len({id(f) for f in fns}) == N                             # distinct fns
    assert len({id(f.__closure__[0]) for f in fns}) == N              # distinct CELLS
    assert len({id(f.__closure__[0].cell_contents) for f in fns}) == 1  # SAME values
    assert all(f.__code__ is work.__wrapped__.__code__ for f in fns)    # SHARED code


def test_hot_noop_on_module_level_def():
    def plain(x):                             # captures nothing -> already scales
        return x + 1
    assert stackweave.hot(plain) is plain        # returned unchanged, a true no-op


def test_hot_noop_on_rebound_capture():
    total = 0

    def acc():
        nonlocal total                        # REBINDS a capture -> unsafe to split
        total += 1
    assert stackweave.hot(acc) is acc            # left shared, not split


def test_hot_noop_on_non_function():
    class C:
        def __call__(self):
            return 7
    c = C()
    assert stackweave.hot(c) is c                # passthrough, no crash


def test_hot_under_mn_scheduler():
    out = bytearray(64)                       # captured, mutated in place (safe)

    @stackweave.hot
    def w(i):
        out[i] = (i * 3) & 0xff               # distinct slot -> race-free

    def root():
        for i in range(64):
            stackweave.fiber(w, i)

    stackweave.run(4, root)
    assert all(out[i] == ((i * 3) & 0xff) for i in range(64)), bytes(out)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _name)
    print("all hot tests passed")
