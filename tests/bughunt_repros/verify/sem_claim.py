import stackweave
from stackweave.sync import Semaphore
def main():
    sem = Semaphore(1)
    sem.acquire()                 # only permit taken
    ok = sem.acquire(False)       # threading-style non-blocking acquire
    print("acquire(False) with 0 free permits ->", ok)
    print("held:", sem._held, "limit:", sem._limit)
stackweave.run(1, main)
