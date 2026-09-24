import threading
import stackweave_c

def body():
    for _ in range(100000):
        stackweave_c.yield_()

c = stackweave_c.Coro(body)
lk = threading.Lock()

def spin():
    while True:
        with lk:
            if c.done:
                return
            try:
                c.resume()
            except RuntimeError:
                pass

ts = [threading.Thread(target=spin) for _ in range(8)]
for t in ts: t.start()
for t in ts: t.join()
print('locked control: done without crash')
