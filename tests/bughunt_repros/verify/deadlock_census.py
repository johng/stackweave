import stackweave, time
def cpu():
    t0 = time.monotonic(); x = 0
    while time.monotonic() - t0 < 1.0: x += 1
    print("cpu fiber finished fine, x =", x)
stackweave.mn_init(2)
stackweave.mn_fiber(cpu)
print("mn_run returned", stackweave.mn_run())
stackweave.mn_fini()
