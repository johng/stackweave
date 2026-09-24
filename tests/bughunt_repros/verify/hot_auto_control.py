import stackweave
results = []
def make(tag):
    def handler():
        results.append(tag)
    return handler
def main():
    for i in range(100):
        stackweave.fiber(make(i))
    stackweave.sleep(1.0)
stackweave.run(2, main)
print(len(set(results)), "distinct tags (expected 100), ran", len(results))
