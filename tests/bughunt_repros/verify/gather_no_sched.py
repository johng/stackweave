import stackweave
res = stackweave.gather(lambda: 1, lambda: 2)   # never returns?
print(res)
