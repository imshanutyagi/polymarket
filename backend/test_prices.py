#!/usr/bin/env python3
"""Quick test: compare what /book, /price, and /midpoint return for the current market."""
import asyncio, httpx, json, sys

async def main():
    # Get current market slug from Gamma
    async with httpx.AsyncClient(timeout=10.0) as hc:
        # Find active BTC hourly market
        print("=== Finding active BTC hourly market ===")
        g = await hc.get("https://gamma-api.polymarket.com/events?slug=bitcoin-up-or-down-hourly&active=true&closed=false&limit=5")
        if g.status_code != 200:
            # Try searching
            g = await hc.get("https://gamma-api.polymarket.com/events?tag=crypto&active=true&closed=false&limit=20")

        # Also try specific slug patterns
        from datetime import datetime
        now = datetime.utcnow()
        # Try common slug patterns
        slugs_to_try = []
        for hour in range(24):
            ampm = "am" if hour < 12 else "pm"
            h12 = hour if hour <= 12 else hour - 12
            if h12 == 0: h12 = 12
            slug = f"bitcoin-up-or-down-march-{now.day}-2026-{h12}{ampm}-et"
            slugs_to_try.append(slug)

        up_token = None
        down_token = None
        found_slug = None

        for slug in slugs_to_try:
            try:
                resp = await hc.get(f"https://gamma-api.polymarket.com/events?slug={slug}")
                if resp.status_code == 200:
                    data = resp.json()
                    if data and len(data) > 0:
                        for m in data[0].get("markets", []):
                            if not m.get("closed") and m.get("active"):
                                tokens = json.loads(m.get("clobTokenIds", "[]"))
                                outcomes = json.loads(m.get("outcomes", "[]"))
                                prices = json.loads(m.get("outcomePrices", "[]"))
                                if len(tokens) >= 2:
                                    yes_i = next((i for i, o in enumerate(outcomes) if o.lower() in ("yes", "up")), 0)
                                    no_i = next((i for i, o in enumerate(outcomes) if o.lower() in ("no", "down")), 1)
                                    up_token = tokens[yes_i]
                                    down_token = tokens[no_i]
                                    found_slug = slug
                                    print(f"Found active market: {slug}")
                                    print(f"Gamma display prices: UP={round(float(prices[yes_i])*100)}¢  DOWN={round(float(prices[no_i])*100)}¢")
                                    break
                        if up_token:
                            break
            except:
                continue

        if not up_token:
            print("ERROR: Could not find active market. Trying tokens.json...")
            try:
                with open("tokens.json") as f:
                    toks = json.load(f)
                    up_token, down_token = toks[0], toks[1]
                    print(f"Using tokens.json: UP={up_token[:20]}... DOWN={down_token[:20]}...")
            except:
                print("No tokens.json found either. Exiting.")
                return

        print(f"\n=== Testing all CLOB endpoints ===")
        print(f"UP token:   {up_token[:30]}...")
        print(f"DOWN token: {down_token[:30]}...")

        # 1. /book endpoint
        print(f"\n--- /book (orderbook best ask = Polymarket buy button price) ---")
        try:
            bu, bd = await asyncio.gather(
                hc.get(f"https://clob.polymarket.com/book?token_id={up_token}"),
                hc.get(f"https://clob.polymarket.com/book?token_id={down_token}"),
            )
            if bu.status_code == 200:
                ud = bu.json()
                asks = ud.get("asks", [])
                bids = ud.get("bids", [])
                if asks:
                    best_ask = min(float(a["price"]) for a in asks)
                    print(f"  UP best ask: {best_ask} = {round(best_ask*100)}¢  ({len(asks)} asks, {len(bids)} bids)")
                    # Show top 3 asks
                    sorted_asks = sorted(asks, key=lambda a: float(a["price"]))[:3]
                    for a in sorted_asks:
                        print(f"    ask: price={a['price']} size={a['size']}")
                else:
                    print(f"  UP: NO ASKS ({len(bids)} bids)")
            else:
                print(f"  UP /book returned status {bu.status_code}")

            if bd.status_code == 200:
                dd = bd.json()
                asks = dd.get("asks", [])
                bids = dd.get("bids", [])
                if asks:
                    best_ask = min(float(a["price"]) for a in asks)
                    print(f"  DN best ask: {best_ask} = {round(best_ask*100)}¢  ({len(asks)} asks, {len(bids)} bids)")
                    sorted_asks = sorted(asks, key=lambda a: float(a["price"]))[:3]
                    for a in sorted_asks:
                        print(f"    ask: price={a['price']} size={a['size']}")
                else:
                    print(f"  DN: NO ASKS ({len(bids)} bids)")
            else:
                print(f"  DN /book returned status {bd.status_code}")
        except Exception as e:
            print(f"  /book ERROR: {e}")

        # 2. /price endpoint
        print(f"\n--- /price (computed price for BUY side) ---")
        try:
            pu, pd_r = await asyncio.gather(
                hc.get(f"https://clob.polymarket.com/price?token_id={up_token}&side=BUY"),
                hc.get(f"https://clob.polymarket.com/price?token_id={down_token}&side=BUY"),
            )
            if pu.status_code == 200:
                p = float(pu.json().get("price", 0))
                print(f"  UP: {p} = {round(p*100)}¢")
            else:
                print(f"  UP /price returned status {pu.status_code}: {pu.text[:100]}")
            if pd_r.status_code == 200:
                p = float(pd_r.json().get("price", 0))
                print(f"  DN: {p} = {round(p*100)}¢")
            else:
                print(f"  DN /price returned status {pd_r.status_code}: {pd_r.text[:100]}")
        except Exception as e:
            print(f"  /price ERROR: {e}")

        # 3. /midpoint endpoint
        print(f"\n--- /midpoint (bid-ask midpoint) ---")
        try:
            mu, md_r = await asyncio.gather(
                hc.get(f"https://clob.polymarket.com/midpoint?token_id={up_token}"),
                hc.get(f"https://clob.polymarket.com/midpoint?token_id={down_token}"),
            )
            if mu.status_code == 200:
                m = float(mu.json().get("mid", 0))
                print(f"  UP: {m} = {round(m*100)}¢")
            else:
                print(f"  UP /midpoint returned status {mu.status_code}: {mu.text[:100]}")
            if md_r.status_code == 200:
                m = float(md_r.json().get("mid", 0))
                print(f"  DN: {m} = {round(m*100)}¢")
            else:
                print(f"  DN /midpoint returned status {md_r.status_code}: {md_r.text[:100]}")
        except Exception as e:
            print(f"  /midpoint ERROR: {e}")

        print(f"\n=== SUMMARY ===")
        print("The Polymarket 'Buy Up X¢' button shows the /book best ask price.")
        print("If /book best ask matches Polymarket but bot shows different, the bot is using /price or /midpoint as fallback.")

asyncio.run(main())
