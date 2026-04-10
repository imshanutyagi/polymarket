import urllib.request
import json
import ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

req = urllib.request.Request("https://gamma-api.polymarket.com/events?limit=400&active=true&closed=false")
req.add_header('User-Agent', 'Mozilla/5.0')
try:
    response = urllib.request.urlopen(req, context=ctx)
    data = json.loads(response.read().decode('utf-8'))
    results = []
    for item in data:
        title = item.get("title", "")
        if ("Bitcoin" in title or "BTC" in title) and ("Price" in title or ":" in title):
            results.append({"title": title, "slug": item.get("slug", "")})
    print(json.dumps(results[:20], indent=2))
except Exception as e:
    print(e)
