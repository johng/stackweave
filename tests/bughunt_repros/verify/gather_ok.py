import stackweave
def main():
    print(stackweave.gather(lambda: 1, lambda: 2))
stackweave.run(1, main)
