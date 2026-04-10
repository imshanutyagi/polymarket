import asyncio, websockets, json
async def test():
    uri = 'ws://127.0.0.1:8000/ws'
    try:
        async with websockets.connect(uri) as ws:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                data = json.loads(msg)
                if data['type'] == 'state_update':
                    market = data['data']['market']
                    print(f'up_price: {market["up_price"]}, down_price: {market["down_price"]}')
                    return
    except Exception as e:
        print(f'Error: {e}')
asyncio.run(test())
