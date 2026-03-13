
import httpx, asyncio, urllib.parse, json; 
async def test():
    slug = 'bitcoin-up-or-down-march-7-4am-et'
    target = urllib.parse.quote(f'https://gamma-api.polymarket.com/events?slug={slug}')
    url = f'https://api.codetabs.com/v1/proxy/?quest={target}&cb=126'
    async with httpx.AsyncClient() as client:
        res = await client.get(url, timeout=10.0)
        data = res.json()
        with open('dump_hourly.json', 'w') as f:
            json.dump(data, f, indent=2)
asyncio.run(test())
