"""Small stackweave workload for the valgrind memcheck run (S4). Single-hub chan
ping-pong + spawn churn -- enough to exercise the scheduler, channels, stack
paint/pool, and the recycle path under memcheck without a long run."""
import sys
sys.path.insert(0, "src")
import stackweave_c

for _ in range(3):
    a, b = stackweave_c.Chan(), stackweave_c.Chan()

    def pinger():
        for i in range(200):
            a.send(i)
            b.recv()

    def ponger():
        for _ in range(200):
            v, _ = a.recv()
            b.send(v)

    stackweave_c.fiber(pinger)
    stackweave_c.fiber(ponger)
    stackweave_c.run()
print("workload done")
