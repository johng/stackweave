# os.fork() from an M:N fiber: in the child, install a Python signal handler,
# raise the signal, then run pure bytecode.  Does the handler run promptly?
from prelude import *
import signal, time as _t
def child_body():
    hits = []
    try:
        signal.signal(signal.SIGALRM, lambda *a: hits.append(_t.perf_counter()))
    except Exception as e:
        os.write(1, ("child: signal.signal failed %r\n" % e).encode()); os._exit(2)
    t0 = _t.perf_counter()
    signal.setitimer(signal.ITIMER_REAL, 0.05)
    end = t0 + 1.0
    while _t.perf_counter() < end and not hits:
        pass
    lat = (hits[0] - t0) if hits else None
    os.write(1, ("child: main_thread=%s handler_latency=%s\n" % (
        threading.current_thread() is threading.main_thread(), lat)).encode())
    os._exit(0 if hits else 1)
def main():
    pid = os.fork()
    if pid == 0:
        child_body()
    _, st = os.waitpid(pid, 0)
    print("child exit", os.waitstatus_to_exitcode(st), flush=True)
    print("PASS" if os.waitstatus_to_exitcode(st) == 0 else "FAIL", flush=True)
mode = sys.argv[1] if len(sys.argv) > 1 else "mn"
if mode == "mn":
    stackweave.run(2, main)
elif mode == "thread":   # control without stackweave: fork from a plain non-main thread
    t = threading.Thread(target=main); t.start(); t.join()
else:
    main()
