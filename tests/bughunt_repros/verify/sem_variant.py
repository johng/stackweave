import stackweave
from stackweave.sync import Semaphore
state = {"b": False, "b2": False}
def main():
    sem = Semaphore(2)
    sem.acquire(1)
    def a():
        r = sem.acquire(2, timeout=0.3)
        print("a acquire ->", r)
    def b():
        sem.acquire(1)
        state["b"] = True
    stackweave.fiber(a)
    stackweave.sleep(0.05)
    stackweave.fiber(b)
    stackweave.sleep(3.0)
    print("after 3s, b_acquired =", state["b"])
    sem.release(1)          # a future release finally grants B
    stackweave.sleep(0.1)
    print("after release, b_acquired =", state["b"])
stackweave.run(1, main)
