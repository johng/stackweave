import stackweave, stackweave.time
violations = 0
def main():
    global violations
    for i in range(3000):
        t = stackweave.time.NewTimer(0.001)
        stackweave.sleep(0.001)
        stopped = t.Stop()
        stackweave.sleep(0.002)
        if stopped and t.c.try_recv() is not None:
            violations += 1
    print(violations, "violations")
stackweave.run(4, main)
