
import httpx, asyncio, urllib.parse, json
async def test():
    target = urllib.parse.quote('https://gamma-api.polymarket.com/events?slug=bitcoin-up-or-down-march-7-3am-et')
    url = 'https://api.codetabs.com/v1/proxy/?quest=' + target
    async with httpx.AsyncClient() as client:
        res = await client.get(url, timeout=10.0)
        data = res.json()
        if isinstance(data, list) and len(data) > 0:
            with open('dump.json', 'w') as f:
                json.dump(data[0], f, indent=2)
            print('Dumped successfully!')
asyncio.run(test())
