import os
os.environ["STACKWEAVE_HOT_AUTO"] = "1"
os.environ["STACKWEAVE_HOT_AUTO_AFTER"] = "8"
import stackweave
results = []
def make(tag):
    def handler():
        results.append(tag)
    return handler
def main():
    for i in range(100):
        stackweave.fiber(make(i))   # 100 DISTINCT closures
    stackweave.sleep(1.0)
stackweave.run(2, main)
print(len(set(results)), "distinct tags (expected 100)")
from collections import Counter
c = Counter(results)
print("ran", len(results), "handlers; most common:", c.most_common(3))
