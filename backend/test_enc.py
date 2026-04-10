
import httpx, asyncio, urllib.parse; 
async def test():
    try:
        target = urllib.parse.quote('https://gamma-api.polymarket.com/events?slug=bitcoin-up-or-down-march-7-2am-et')
        url = 'https://api.codetabs.com/v1/proxy/?quest=' + target
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            data = res.json()
            if isinstance(data, list) and len(data) > 0:
                print('Event Title:', data[0].get('title'))
                if 'markets' in data[0]:
                    print('Outcomes:', data[0]['markets'][0].get('outcomes'))
                    print('Prices:', data[0]['markets'][0].get('outcomePrices'))
            else:
                print('Not a valid event array!')
    except Exception as e:
        print('Error:', e)
asyncio.run(test())
