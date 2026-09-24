import sys, stackweave
from stackweave.sync import Semaphore
state = {"b": False}
def main():
    sem = Semaphore(2)
    sem.acquire(1)                       # long-lived holder
    def a():
        sem.acquire(2, timeout=0.3)      # times out at the front
    def b():
        sem.acquire(1)                   # fits, but queued behind a
        state["b"] = True
    stackweave.fiber(a)
    stackweave.sleep(0.05)
    stackweave.fiber(b)
    stackweave.sleep(1.0)
    print("b_acquired =", state["b"], "(expected True)")
    sem.release(1)  # unwedge for clean exit
stackweave.run(1, main)
