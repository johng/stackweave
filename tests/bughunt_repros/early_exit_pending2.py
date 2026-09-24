import stackweave, time, sys, os

TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 50
SPAWNS = int(sys.argv[2]) if len(sys.argv) > 2 else 30000

def noop():
    pass

for t in range(TRIALS):
    stackweave.mn_init(6)
    state = {"done": False}
    def root():
        for i in range(SPAWNS):
            stackweave.mn_fiber(noop)
        state["done"] = True
    stackweave.mn_fiber(root)
    stackweave.mn_run()
    if not state["done"]:
        deadline = time.time() + 5.0
        while not state["done"] and time.time() < deadline:
            time.sleep(0.005)
        print(f"trial {t}: BUG -- mn_run returned while root fiber still running; "
              f"root finished afterwards: {state['done']}", flush=True)
        stackweave.mn_fini()
        sys.exit(1)
    stackweave.mn_fini()
print("no bug seen in", TRIALS, "trials")
