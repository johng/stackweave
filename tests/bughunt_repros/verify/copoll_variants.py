import select, sys, os, time
import stackweave

mode = sys.argv[1]

def main():
    r, w = os.pipe()
    def poller():
        p = select.poll(); p.register(r, select.POLLIN)
        t0 = time.monotonic()
        if mode == "finite":
            res = p.poll(3000)   # 3s cap on the worker occupation
        else:
            res = p.poll(None)
        print("poll done after %.2fs:" % (time.monotonic()-t0), res, flush=True)
    def writer():
        stackweave.sleep(0.2)
        print("writer: before offload", flush=True)
        if mode != "nooffload":
            stackweave.monkey.offload(time.sleep, 0.05)
        print("writer: after offload", flush=True)
        os.write(w, b"x"); print("writer done", flush=True)
    stackweave.fiber(poller); stackweave.fiber(writer)

stackweave.monkey.patch()
stackweave.run(1, main)
print("ALL DONE", flush=True)
