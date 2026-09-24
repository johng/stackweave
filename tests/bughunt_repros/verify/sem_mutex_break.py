import threading
import stackweave
from stackweave.sync import Semaphore
from stackweave.monkey import CoSemaphore

# Baseline: stdlib threading semantics
t = threading.Semaphore(1)
t.acquire()
print("threading.Semaphore acquire(False) with 0 free ->", t.acquire(False))

def main():
    cs = CoSemaphore(1)
    cs.acquire()
    print("monkey.CoSemaphore acquire(False) with 0 free ->", cs.acquire(False))

    # mutual exclusion break with sync.Semaphore
    sem = Semaphore(1)
    inside = []
    def worker(i):
        if sem.acquire(False):   # threading-style non-blocking acquire
            inside.append(i)
            stackweave.sleep(0.2)   # hold the "permit"
            sem.release()
    for i in range(4):
        stackweave.fiber(worker, i)
    stackweave.sleep(0.1)
    print("fibers simultaneously in critical section guarded by Semaphore(1):", len(inside), inside)
    stackweave.sleep(0.3)
stackweave.run(2, main)
