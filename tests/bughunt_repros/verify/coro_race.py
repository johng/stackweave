import threading
import stackweave_c

def body():
    for _ in range(100000):
        stackweave_c.yield_()

c = stackweave_c.Coro(body)

def spin():
    while not c.done:
        try:
            c.resume()
        except RuntimeError:
            pass   # guard fired cleanly -- fine

ts = [threading.Thread(target=spin) for _ in range(8)]
for t in ts: t.start()
for t in ts: t.join()
print('done without crash')
