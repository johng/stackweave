"""Timer.Stop() does not wake the backing fire fiber, so run() cannot return
until the ORIGINAL deadline even though the timer was stopped.
Go's timer.Stop removes the timer from the heap immediately."""
import time as wall
import stackweave
import stackweave.time

def main():
    t = stackweave.time.NewTimer(3.0)
    stopped = t.Stop()
    print("Stop() ->", stopped)

t0 = wall.monotonic()
stackweave.run(1, main)
dt = wall.monotonic() - t0
print("run() took %.2fs (expected ~0s; the timer was stopped immediately)" % dt)
if dt > 2.0:
    print("BUG: run() blocked until the stopped timer's original deadline")
