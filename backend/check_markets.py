import httpx
import json

headers = {"User-Agent": "Mozilla/5.0"}
try:
    response = httpx.get("https://gamma-api.polymarket.com/events?limit=100&active=true", headers=headers, timeout=15.0)
    data = response.json()
    count = 0
    for event in data:
        if "Bitcoin" in event.get("title", ""):
            print(event["title"])
            print(event["slug"])
            print(event["endDate"])
            print("---")
            count += 1
            if count > 5: break
except Exception as e:
    print("Error:", e)
