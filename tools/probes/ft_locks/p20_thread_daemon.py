# threading.Thread started inside an M:N fiber inherits daemon=True from the
# hub's _DummyThread, so interpreter exit does not wait for it.
from prelude import *
import time as _t
out_path = sys.argv[1]
def main():
    def work():
        _t.sleep(0.5)
        with open(out_path, "w") as f: f.write("done")
    t = threading.Thread(target=work)
    print("daemon:", t.daemon, flush=True)
    t.start()
stackweave.run(2, main)
print("main returning", flush=True)
