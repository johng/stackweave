import os, signal, threading, time
import stackweave
def main():
    def busy():
        i = 0
        while True:
            i += 1
            if i % 10000 == 0: stackweave.yield_now()
    for _ in range(3): stackweave.fiber(busy)
def killer():
    for _ in range(10):
        time.sleep(0.5); os.kill(os.getpid(), signal.SIGINT)
threading.Thread(target=killer, daemon=True).start()
t0 = time.time()
try:
    stackweave.run(1, main)
    print('run returned normally (KeyboardInterrupt swallowed) after %.1fs' % (time.time()-t0))
except KeyboardInterrupt:
    print('KeyboardInterrupt propagated after %.1fs' % (time.time()-t0))
