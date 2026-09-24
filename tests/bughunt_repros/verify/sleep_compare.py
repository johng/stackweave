import time as wall
import stackweave, stackweave.time

# 1. stackweave.sleep outside a fiber (documented fallback path)
t0 = wall.monotonic()
stackweave.sleep(0.3)
print("stackweave.sleep(0.3) outside fiber: %.3fs" % (wall.monotonic() - t0))

# 2. stackweave.time.Sleep outside a fiber (claimed no-op)
t0 = wall.monotonic()
stackweave.time.Sleep(0.3)
print("stackweave.time.Sleep(0.3) outside fiber: %.3fs" % (wall.monotonic() - t0))

# 3. Sanity: Sleep inside a fiber works
def f():
    t0 = wall.monotonic()
    stackweave.time.Sleep(0.3)
    print("stackweave.time.Sleep(0.3) inside fiber: %.3fs" % (wall.monotonic() - t0))

import stackweave_c
stackweave_c.fiber(f)
stackweave_c.run()
