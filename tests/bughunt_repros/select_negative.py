import socket, select, sys
def scenario(tag):
    a, b = socket.socketpair()
    try:
        r = select.select([b], [], [], -1)
        print(tag, "returned", r, flush=True)
    except ValueError as e:
        print(tag, "ValueError:", e, flush=True)
    except Exception as e:
        print(tag, type(e).__name__, e, flush=True)
    a.close(); b.close()
if sys.argv[1] == "stock":
    scenario("stock:")
else:
    import stackweave
    def main(): stackweave.fiber(lambda: scenario("patched:"))
    stackweave.monkey.patch(); stackweave.run(2, main)
