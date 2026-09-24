import stackweave, stackweave.context as ctx
def main():
    parent, _ = ctx.WithCancel(ctx.Background())
    for i in range(10000):
        child, cancel = ctx.WithCancel(parent)
        cancel()
    print("len(parent._children) =", len(parent._children))
stackweave.run(1, main)
