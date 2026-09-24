import time as wall, stackweave, stackweave.context as ctx
def main():
    c, cancel = ctx.WithTimeout(ctx.Background(), 3.0)
    stackweave.sleep(0.2)
    cancel()
    print("deadline_g =", c._deadline_g)
t0 = wall.monotonic()
stackweave.run(1, main)
print("run(1) took %.2fs" % (wall.monotonic() - t0))
