import sys, time
import stackweave
# preamble: two normal M:N cycles first (like gc_weakref.py did)
stackweave.run(4, lambda: None)
stackweave.run(4, lambda: None)
def main():
    ch = stackweave.Chan(0)
    def parked():
        ch.recv()
    stackweave.fiber(parked)
t0 = time.time()
n = stackweave.run(4, main)
print("run returned n=%r after %.1fs" % (n, time.time() - t0))
