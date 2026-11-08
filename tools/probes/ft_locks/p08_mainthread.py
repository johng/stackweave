# Main-thread checks from inside fibers: signal.signal, set_wakeup_fd,
# threading.main_thread()/current_thread(), asyncio.run's signal handling.
from prelude import *
patched = len(sys.argv) > 1 and sys.argv[1] == "patch"
if patched:
    import stackweave.monkey as monkey; monkey.patch()
import signal, asyncio
def main():
    out = {}
    def probe():
        r = {}
        r["is_main"] = threading.current_thread() is threading.main_thread()
        r["cur"] = type(threading.current_thread()).__name__
        try:
            old = signal.signal(signal.SIGUSR1, lambda *a: None); signal.signal(signal.SIGUSR1, old); r["signal.signal"] = "ok"
        except Exception as e: r["signal.signal"] = repr(e)[:70]
        try:
            signal.set_wakeup_fd(-1); r["set_wakeup_fd"] = "ok"
        except Exception as e: r["set_wakeup_fd"] = repr(e)[:70]
        try:
            async def co(): return 1
            asyncio.run(co()); r["asyncio.run"] = "ok"
        except Exception as e: r["asyncio.run"] = repr(e)[:70]
        return r
    out["main_fiber"] = probe()
    done = stackweave.Chan(1)
    stackweave.fiber(lambda: done.send(probe()))
    out["child_fiber"] = done.recv()[0]
    for k, v in out.items(): print(k, v, flush=True)
print("outside run:", threading.current_thread() is threading.main_thread())
stackweave.run(2, main)
