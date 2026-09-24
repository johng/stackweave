import os
os.environ["STACKWEAVE_STACK_ARENA"] = "0"
import stackweave, stackweave_c as rc
res=[]
def worker():
    res.append(sum(range(10)))
def main():
    rc.mn_fiber(worker, 32768)
    for _ in range(1000):
        rc.sched_yield()
        if res: break
    print("res:", res)
stackweave.run(2, main)
print("DONE")
