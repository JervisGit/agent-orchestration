import json, sys

data = json.load(sys.stdin)
for m in data:
    if "gpt" in m["name"]:
        skus = [s["name"] for s in m.get("skus", [])]
        print(f'{m["name"]:25} {m["version"]:15} {skus}')
