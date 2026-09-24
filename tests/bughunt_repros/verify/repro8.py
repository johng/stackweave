import time
import stackweave
def main():
    ch = stackweave.Chan(0)
    def parked():
        ch.recv()   # no sender ever
    stackweave.fiber(parked)
t0 = time.time()
n = stackweave.run(8, main)   # default STACKWEAVE_DEADLOCK (warn): expect DEADLOCK banner on stderr within ~200ms
print('run returned', n, time.time() - t0)
