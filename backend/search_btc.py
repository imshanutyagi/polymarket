import urllib.request
import json
import urllib.parse

q = urllib.parse.quote("https://gamma-api.polymarket.com/events?search=Bitcoin Price&limit=100&active=true&closed=false")
url = f"https://api.codetabs.com/v1/proxy/?quest={q}"
response = urllib.request.urlopen(url)
data = json.loads(response.read().decode('utf-8'))

results = []
for item in data:
    title = item.get("title", "")
    results.append({"title": title, "slug": item.get("slug", "")})

with open("gamma_btc_results.json", "w") as f:
    json.dump(results, f, indent=2)
