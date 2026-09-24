import stackweave, stackweave.sync as gsync

# Control 1: stackweave.fiber (runtime._fiber_full) under M:N run(2)
ran1 = {"v": False}
def w1(): ran1["v"] = True
def main1():
    stackweave.fiber(w1)
    stackweave.sleep(0.3)
stackweave.run(2, main1)
print("control stackweave.fiber under run(2):", ran1["v"], "(expect True)")
