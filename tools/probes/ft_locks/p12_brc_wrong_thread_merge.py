# Evidence for a biased-refcount merge on the wrong OS thread [2].
# _Py_brc_merge_refcounts moves a fiber's shared merge queue to its private
# local_objects_to_merge stack and pops it, deallocating as it goes.  If a
# __del__ in that loop parks and the fiber resumes on another hub, whatever is
# still on the stack is merged there, off its owner (_Py_ExplicitMergeRefcount
# writes ob_ref_local non-atomically).  Fixed by runloom_iframe_brc_release
# draining the local stack at the park (stats()["brc_release_local_drains"]).
#
# Deterministic trigger: a plain OS thread drops B, B, B, A while the producer
# fiber spins in C on its hub until the dropper sets a flag: no bytecode, so no
# eval-breaker check, and its state stays ATTACHED at the head of the hub's
# bucket, so every drop queues to it.  (Not a lock: a fiber's contended acquire
# parks it, and drops made while it is parked go to the hub's own state.  Not
# os.read: it detaches the state, and from CPython 3.15.0rc3 a dropper that
# finds its owner detached merges the queue itself, on the dropper's thread.)
# The producer's next eval-breaker check merges all four in one batch, A on
# top.  A's __del__ parks at once and is resumed on
# another hub by a pin, with the B's still on the local stack.  B checks the
# thread at the first line of its __del__.  (An earlier version had a fiber on
# another hub drop the objects while the producer spun: the stack was almost
# always emptied by a nested merge before the park, and its oracle counted
# B's whose __del__ body ran after a nested merge had moved the fiber -- merged
# on their owner, then moved -- so it FAILed with or without the bug.)
from prelude import *
import collections, functools, gc, itertools, operator, queue
gc.disable()
H = int(sys.argv[1]) if len(sys.argv) > 1 else 4
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 300
_watchdog(110)
wake = stackweave.Chan(0)
stats = {"a_moved": 0, "a_ahead": 0, "b_del": 0, "b_off_owner": 0,
         "b_off_owner_nofiber": 0, "b_on_dropper": 0, "round_b0": 0,
         "lost": 0, "dropper": None}
dropped = bytearray(1)
poll = functools.partial(operator.getitem, dropped, 0)
def send_or_give_up(ch, value, secs):
    end = time.monotonic() + secs
    while True:
        try:
            ch.send(value); return True
        except RuntimeError:
            if time.monotonic() > end: return False
            time.sleep(0.0005)
class A:
    __slots__ = ()
    def __del__(self):
        g = stackweave_c.current_g()
        if g is None: return
        if stats["b_del"] == stats["round_b0"]: stats["a_ahead"] += 1
        before = threading.get_ident()
        g.pin((stackweave_c.mn_current_hub() + 1) % stackweave_c.mn_hub_count())
        try:
            wake.recv()
        finally:
            g.pin(None)
        if threading.get_ident() != before: stats["a_moved"] += 1
class B:
    __slots__ = ("owner",)
    def __init__(self): self.owner = threading.get_ident()
    def __del__(self):
        stats["b_del"] += 1
        if threading.get_ident() == stats["dropper"]:
            stats["b_on_dropper"] += 1     # CPython's merge-on-behalf, not the bug
        elif threading.get_ident() != self.owner:
            stats["b_off_owner"] += 1
            if stackweave_c.current_g() is None: stats["b_off_owner_nofiber"] += 1
def main():
    inbox = queue.SimpleQueue()
    def dropper():
        stats["dropper"] = threading.get_ident()
        while True:
            v = inbox.get()
            if v is None: return
            while v: v.pop(0)               # drop B, B, B, then A
            del v
            dropped[0] = 1
            if not send_or_give_up(wake, 1, 5): stats["lost"] += 1
    t = threading.Thread(target=dropper, daemon=True); t.start()
    wg = stackweave.WaitGroup(); wg.add(1)
    def producer():
        for _ in range(ROUNDS):
            if stats["lost"]: break
            stats["round_b0"] = stats["b_del"]
            dropped[0] = 0
            inbox.put([B(), B(), B(), A()])
            while not dropped[0]:           # spin in C, attached, no bytecode
                collections.deque(itertools.islice(iter(poll, 1), 1_000_000), maxlen=0)
        inbox.put(None); wg.done()
    stackweave_c.mn_fiber(producer, hub=0)
    wg.wait(); t.join()
    stackweave.sleep(0.2)
    print(stats, flush=True)
    st = stackweave_c.stats(); print({k: v for k, v in st.items() if k.startswith("brc")}, flush=True)
    print("PASS" if stats["b_off_owner"] == 0 and stats["lost"] == 0 and stats["b_on_dropper"] == 0 else "FAIL: setup (lost handshake or merge-on-behalf)" if stats["b_off_owner"] == 0 else "FAIL: %d objects merged+freed off their owner thread" % stats["b_off_owner"], flush=True)
stackweave.run(H, main)
