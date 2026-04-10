import urllib.request
import json

url = "https://api.codetabs.com/v1/proxy/?quest=https%3A%2F%2Fgamma-api.polymarket.com%2Fevents%3Flimit%3D500%26active%3Dtrue%26closed%3Dfalse"
response = urllib.request.urlopen(url)
data = json.loads(response.read().decode('utf-8'))

results = []
for item in data:
    title = item.get("title", "")
    if "Bitcoin" in title or "BTC" in title:
        results.append({"title": title, "slug": item.get("slug", "")})

with open("gamma_btc_results.json", "w") as f:
    json.dump(results, f, indent=2)
