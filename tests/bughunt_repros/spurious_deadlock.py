# With STACKWEAVE_PREEMPT=0 (documented opt-out) sysmon stays off, so
# hub->resume_start_ns is never written.  runloom_mn_has_wakeable_work()
# then cannot see a hub that is mid-resume: a single CPU-bound fiber with
# empty queues makes the deadlock census read "quiescent" -> spurious
# DEADLOCK/STALL report (and a RuntimeError under STACKWEAVE_DEADLOCK=raise)
# while the program is making progress.
import stackweave, time, sys

def cpu():
    t0 = time.monotonic()
    x = 0
    while time.monotonic() - t0 < 1.0:
        x += 1
    print("cpu fiber finished fine, x =", x)

stackweave.mn_init(2)
stackweave.mn_fiber(cpu)
r = stackweave.mn_run()
print("mn_run returned", r)
stackweave.mn_fini()
