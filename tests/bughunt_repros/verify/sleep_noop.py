import time as wall, stackweave.time
t0 = wall.monotonic()
stackweave.time.Sleep(0.5)
print("Sleep(0.5) outside a fiber returned after %.3fs" % (wall.monotonic() - t0))
