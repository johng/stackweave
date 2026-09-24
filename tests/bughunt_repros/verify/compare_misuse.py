import stackweave, stackweave_c
# 1) Chan recv outside run: does it raise a diagnostic?
try:
    stackweave.Chan().recv()
    print("chan recv: returned??")
except BaseException as e:
    print("chan recv raised:", type(e).__name__, e)
# 2) does stackweave_c.fiber outside run raise or silently queue?
g = stackweave_c.fiber(lambda: print("fiber ran"))
print("fiber() returned:", g)
print("mn_hub_count:", stackweave_c.mn_hub_count())
print("current_g:", stackweave_c.current_g())
