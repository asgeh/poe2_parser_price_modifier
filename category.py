import requests, json

BASE = "https://www.pathofexile.com"
data = requests.get(f"{BASE}/api/trade2/data/filters", timeout=30).json()

needles = ["instant", "buyout", "in person", "merchant", "asynchronous", "ange"]

def walk(x, path="root"):
    if isinstance(x, dict):
        # часто текст/лейблы лежат в "text"/"label"/"name"
        hay = " ".join(str(v).lower() for v in x.values() if isinstance(v, (str,int,float)))
        if any(n in hay for n in needles):
            print("\nPATH:", path)
            print(json.dumps(x, ensure_ascii=False, indent=2)[:2000])
        for k,v in x.items():
            walk(v, f"{path}.{k}")
    elif isinstance(x, list):
        for i,v in enumerate(x):
            walk(v, f"{path}[{i}]")

walk(data)