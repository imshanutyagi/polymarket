import urllib.request
import json
import urllib.parse

try:
    url = "https://api.codetabs.com/v1/proxy/?quest=https%3A%2F%2Fgamma-api.polymarket.com%2Fevents%3Flimit%3D1000%26active%3Dtrue%26closed%3Dfalse"
    response = urllib.request.urlopen(url)
    data = json.loads(response.read().decode('utf-8'))

    print("Total active events found:", len(data))
    
    results = []
    for item in data:
        title = item.get("title", "")
        if "BTC" in title or "price" in title.lower():
            results.append({"title": title, "slug": item.get("slug", "")})

    print(json.dumps(results[:30], indent=2))
except Exception as e:
    print(e)
