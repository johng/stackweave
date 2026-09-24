"""stackweave.time correct-usage probe: After, Timer Stop, Ticker + Stop, Reset churn."""
import time
import stackweave
import stackweave_c

def main():
    t0 = time.monotonic()
    ch = stackweave.time.After(0.05)
    v, ok = ch.recv()
    dt = time.monotonic() - t0
    assert ok and 0.04 < dt < 1.0, (v, ok, dt)

    # Timer.Stop cancels
    tm = stackweave.time.NewTimer(0.05)
    assert tm.Stop() is True
    r = stackweave_c.select([("recv", tm.c)], default=True)
    assert r == -1 or r[0] == -1, r

    # Ticker: collect 5 ticks then Stop
    tk = stackweave.time.NewTicker(0.01)
    t1 = time.monotonic()
    for _ in range(5):
        tk.c.recv()
    tk.Stop()
    dt2 = time.monotonic() - t1
    assert 0.04 < dt2 < 2.0, dt2

    # Reset churn
    tm2 = stackweave.time.NewTimer(0.5)
    for _ in range(20):
        tm2.Reset(0.01)
    v, ok = tm2.c.recv()
    print("time probe2 OK After=%.3f ticker5=%.3f" % (dt, dt2))

stackweave.run(4, main)
