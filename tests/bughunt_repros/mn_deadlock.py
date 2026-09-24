import sys, time
import stackweave
def main():
    ch = stackweave.Chan(0)
    def parked():
        ch.recv()
    stackweave.fiber(parked)
t0 = time.time()
try:
    n = stackweave.run(4, main)
    print("run returned n=%r after %.1fs" % (n, time.time() - t0))
except Exception as e:
    print("raised %r after %.1fs" % (e, time.time() - t0))
