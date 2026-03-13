
import urllib.request, json
req = urllib.request.Request('https://clob.polymarket.com/book?token_id=102952434442465853831921512154505779686508008012166613146251174276239297492202', headers={'User-Agent': 'Mozilla/5.0'})
try:
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        print('Bids:', data.get('bids', [])[:2])
        print('Asks:', data.get('asks', [])[:2])
except Exception as e:
    print('err', e)

