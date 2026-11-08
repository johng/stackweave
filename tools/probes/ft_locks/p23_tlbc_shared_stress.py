# Do fibers that share their hub's bytecode copy (stackweave_c.tlbc_shared)
# still compute right when they migrate mid-function?  N fibers run the same
# polymorphic functions -- attribute loads over four layouts, a global whose
# dict keeps changing version, alternating callees, mixed int/float operands,
# alternating iterables -- so every site keeps deopting and re-specializing,
# and park every few iterations (sleep(0), and a wake from a plain OS thread,
# which goes through the global run-queue to whichever hub takes it) so they
# resume on other hubs mid-loop.  Each fiber checks every result against a value
# computed up front.  If two hubs ever ran one copy at once, a torn inline
# cache (a type version from one specialization, an index or a descriptor from
# another) shows up as a wrong value or a crash.
# With `clear`, a fiber also calls sys._clear_internal_caches() (which frees
# the bytecode copies no live thread state holds) every few ms, while the
# workers' frames are parked mid-function on their hubs' copies.
# usage: p23_tlbc_shared_stress.py [hubs=8] [fibers=64] [rounds=200] [clear]
from prelude import *
H = int(sys.argv[1]) if len(sys.argv) > 1 else 8
N = int(sys.argv[2]) if len(sys.argv) > 2 else 64
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 200
CLEAR = len(sys.argv) > 4 and sys.argv[4] == "clear"
_watchdog(300)


class A:
    def __init__(self, v):
        self.x = v; self.y = v + 1


class B:                      # same names, other order: other slot indices
    def __init__(self, v):
        self.y = v + 1; self.z = 0; self.x = v


class C:
    __slots__ = ("pad", "x", "y")
    def __init__(self, v):
        self.pad = None; self.x = v; self.y = v + 1


class D:
    def __init__(self, v):
        self._x = v; self.y = v + 1
    @property
    def x(self):
        return self._x


def f1(a, b):
    return a + b


def f2(a, b):
    return a - b


def gen(n):
    for i in range(n):
        yield i


GV = 7


def kernel(objs, nums, iters, k, ch, wakeq):
    """One round: every site sees several types in turn.  Parks inside the
    loop, so the frame resumes mid-function on whatever hub takes it."""
    acc = 0
    for i in range(k):
        o = objs[i % len(objs)]
        acc += o.x * 3 + o.y                       # LOAD_ATTR: 4 layouts
        n = nums[i % len(nums)]
        acc += int(n + 1.5) if i & 1 else int(1.5 + n)   # BINARY_OP mixes
        fn = f1 if i % 3 else f2
        acc += fn(i, GV)                           # CALL: 2 callees + global
        for v in iters[i % len(iters)]:            # FOR_ITER: list/tuple/range/gen
            acc += v
        if i % 5 == 0:
            stackweave.sleep(0)                    # yield: stealable
        if i % 11 == 0:                            # foreign wake: any hub
            wakeq.put((ch, i))
            acc += ch.recv()[0]
    return acc


objs_vals = [3, 5, 7, 11, 13, 17, 19, 23]


def make_objs(rot):
    kinds = [A, B, C, D]
    out = [kinds[(j + rot) % 4](objs_vals[j]) for j in range(len(objs_vals))]
    return out


class Regen:
    """An iterable that hands out a fresh generator each time."""
    def __init__(self, n):
        self.n = n
    def __iter__(self):
        return gen(self.n)


def main():
    bad = []
    done = bytearray(N)
    import queue
    wakeq = queue.SimpleQueue()
    stop_wakers = []

    def waker():
        while True:
            item = wakeq.get()
            if item is None:
                return
            foreign_send(*item)

    wakers = [threading.Thread(target=waker, daemon=True) for _ in range(4)]
    for t in wakers:
        t.start()
    wg = stackweave.WaitGroup(); wg.add(N + 1)
    K = 64
    nums_base = [1, 2.5, 3, 4.25, 5, 6.75]
    iters_base = [[1, 2, 3], (4, 5), range(3), Regen(4)]
    expected = {}
    for f in range(N):
        objs = make_objs(f)
        nums = nums_base[f % 6:] + nums_base[:f % 6]
        iters = iters_base[f % 4:] + iters_base[:f % 4]
        acc = 0
        for i in range(K):
            v = i % len(objs)
            acc += objs_vals[v] * 3 + objs_vals[v] + 1
            n = nums[i % len(nums)]
            acc += int(n + 1.5) if i & 1 else int(1.5 + n)
            acc += (i + 7) if i % 3 else (i - 7)
            it = iters[i % len(iters)]
            acc += sum(range(it.n)) if isinstance(it, Regen) else sum(it)
            if i % 11 == 0:
                acc += i                              # the waker sends i back
        expected[f] = (objs, nums, iters, acc)
    def worker(f):
        objs, nums, iters, want = expected[f]
        ch = stackweave.Chan(0)
        try:
            for _ in range(ROUNDS):
                got = kernel(objs, nums, iters, K, ch, wakeq)
                if got != want:
                    bad.append((f, got, want))
        finally:
            done[f] = 1
            wg.done()

    for f in range(N):
        stackweave.fiber(lambda f=f: worker(f))

    def churn_globals():
        # Keep changing the module dict's keys version so LOAD_GLOBAL keeps
        # re-specializing; GV itself never changes.
        g = globals()
        j = 0
        t_end = time.monotonic() + 120
        try:
            while sum(done) < N and time.monotonic() < t_end:
                g["_churn_%d" % (j % 64)] = j
                if j % 3 == 0:
                    g.pop("_churn_%d" % ((j * 7) % 64), None)
                j += 1
                if CLEAR and j % 8 == 0:
                    sys._clear_internal_caches()
                stackweave.sleep(0.0005)
        finally:
            wg.done()

    stackweave.fiber(churn_globals)
    t0 = time.monotonic()
    wg.wait()
    for t in wakers:
        wakeq.put(None)
    st = stackweave_c.stats()
    print("hubs=%d fibers=%d rounds=%d clear=%d %.1fs tlbc_shared=%d "
          "tlbc_switches=%d yield_steals=%d" % (H, N, ROUNDS, CLEAR,
                               time.monotonic() - t0,
                               stackweave_c.tlbc_shared,
                               st.get("tlbc_switches", -1),
                               st.get("mn_yield_steals", -1)), flush=True)
    if bad:
        print("FAIL: %d wrong results (fiber, got, want): %r" % (len(bad), bad[:5]),
              flush=True)
        os._exit(1)
    print("PASS", flush=True)


stackweave.run(H, main)
