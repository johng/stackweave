"""context.WithTimeout under M:N: cancel() cannot wake the deadline-waker
fiber because stackweave_c.mn_fiber() returns None, so ctx._deadline_g is None
and cancel() has no handle to call cancel_wait_fd() on.  run(n>1) then blocks
until the ORIGINAL deadline.  (Under run(1) the same code returns instantly.)"""
import time as wall
import stackweave
import stackweave.context as ctx

def main():
    c, cancel = ctx.WithTimeout(ctx.Background(), 3.0)
    stackweave.sleep(0.2)      # let the deadline-waker fiber park in wait_fd first
    cancel()
    print("cancelled; err =", c.err(), " deadline_g =", c._deadline_g)

t0 = wall.monotonic()
stackweave.run(2, main)
dt = wall.monotonic() - t0
print("run(2) took %.2fs (expected ~0s: the ctx was cancelled immediately)" % dt)
if dt > 2.0:
    print("BUG: cancelled WithTimeout kept mn_run alive until the deadline")
