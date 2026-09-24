"""select.select (cooperative epoll path) also ignores the CANCELLED sentinel:
a fiber parked in select.select survives cancel_all_parked -> teardown hang."""
import socket, select, sys, time
import stackweave, stackweave_c

def main():
    a, b = socket.socketpair()
    state = {}
    def selector():
        try:
            r = select.select([b], [], [])   # no timeout
            state["out"] = ("ready", r)
        except Exception as e:
            state["out"] = ("exc", type(e).__name__, str(e))
    stackweave.fiber(selector)
    stackweave.sleep(0.3)
    n = stackweave_c.cancel_all_parked()
    print("cancelled %d parked" % n, flush=True)
    stackweave.sleep(0.5)
    print("selector state:", state.get("out", "STILL PARKED"), flush=True)

stackweave.monkey.patch()
stackweave.run(2, main)
print("run() returned", flush=True)
