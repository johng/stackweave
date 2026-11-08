# Stock asyncio loop (debug=True, as in IsolatedAsyncioTestCase / -X dev) run
# inside an M:N fiber with monkey.patch: its selector wait parks, the fiber can
# resume on another hub, and BaseEventLoop._check_thread compares get_ident().
from prelude import *
import stackweave.monkey as monkey; monkey.patch()
import asyncio
def main():
    res = {}
    def runner():
        async def co():
            hubs = set()
            for i in range(200):
                await asyncio.sleep(0.001)
                hubs.add(threading.get_ident())
                asyncio.get_running_loop().call_soon(lambda: None)
            return len(hubs)
        try:
            res["os_threads_seen"] = asyncio.run(co(), debug=True); res["ok"] = True
        except Exception as e:
            res["err"] = repr(e)[:140]
    done = stackweave.Chan(1)
    stackweave.fiber(lambda: (runner(), done.send(1)))
    done.recv()
    print(res, flush=True)
    print("PASS" if res.get("ok") else "FAIL", flush=True)
stackweave.run(4, main)
