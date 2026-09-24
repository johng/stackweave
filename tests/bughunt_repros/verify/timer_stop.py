import time as wall, stackweave, stackweave.time
def main():
    t = stackweave.time.NewTimer(3.0)
    print("Stop() ->", t.Stop())
t0 = wall.monotonic()
stackweave.run(1, main)
print("run() took %.2fs (expected ~0s)" % (wall.monotonic() - t0))
