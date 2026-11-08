# Does every fiber get its own thread-local bytecode (TLBC) copy of each code
# object it runs?  N live fibers run one large function, then park; compare
# RSS with sys._clear_internal_caches() (which frees TLBC copies).
from prelude import *
import resource, gc
N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
src = "def big(x):\n" + "".join("    x = (x * %d + %d) %% 1000003\n" % (i, i) for i in range(3000)) + "    return x\n"
ns = {}; exec(src, ns); big = ns["big"]
def rss_mb():
    import subprocess
    return int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())])) / 1024
print("TLBC enabled:", sys._xoptions.get("tlbc", os.environ.get("PYTHON_TLBC", "default")), "code size KB:", len(big.__code__.co_code) // 1024, flush=True)
def main():
    gate = stackweave.Chan(0)
    wg = stackweave.WaitGroup(); wg.add(N)
    def f():
        big(1); big(2)
        wg.done()
        gate.recv()
    base = rss_mb()
    for _ in range(N): stackweave.fiber(f)
    wg.wait()
    live = rss_mb()
    gate.close()
    stackweave.sleep(0.3); gc.collect()
    after = rss_mb()
    sys._clear_internal_caches()
    cleared = rss_mb()
    print("N=%d base=%.0f MB live=%.0f MB after-exit=%.0f MB after-clear_internal_caches=%.0f MB" % (N, base, live, after, cleared), flush=True)
stackweave.run(4, main)
