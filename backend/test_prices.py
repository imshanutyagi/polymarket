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

        # 1. /book endpoint + cross-book pricing
        print(f"\n--- /book (cross-book pricing = Polymarket buy button price) ---")
        up_direct = None; dn_direct = None
        up_cross = None; dn_cross = None
        try:
            bu, bd = await asyncio.gather(
                hc.get(f"https://clob.polymarket.com/book?token_id={up_token}"),
                hc.get(f"https://clob.polymarket.com/book?token_id={down_token}"),
            )
            up_asks, up_bids, dn_asks, dn_bids = [], [], [], []
            if bu.status_code == 200:
                ud = bu.json()
                up_asks = ud.get("asks", [])
                up_bids = ud.get("bids", [])
                if up_asks:
                    up_direct = min(float(a["price"]) for a in up_asks)
                    print(f"  UP direct (best ask): {up_direct} = {round(up_direct*100)}¢  ({len(up_asks)} asks)")
                else:
                    print(f"  UP: NO ASKS ({len(up_bids)} bids)")
                if up_bids:
                    up_best_bid = max(float(b["price"]) for b in up_bids)
                    dn_cross = 1.0 - up_best_bid
                    print(f"  DN cross (1 - UP best bid {up_best_bid}): {dn_cross:.4f} = {round(dn_cross*100)}¢")

            if bd.status_code == 200:
                dd = bd.json()
                dn_asks = dd.get("asks", [])
                dn_bids = dd.get("bids", [])
                if dn_asks:
                    dn_direct = min(float(a["price"]) for a in dn_asks)
                    print(f"  DN direct (best ask): {dn_direct} = {round(dn_direct*100)}¢  ({len(dn_asks)} asks)")
                else:
                    print(f"  DN: NO ASKS ({len(dn_bids)} bids)")
                if dn_bids:
                    dn_best_bid = max(float(b["price"]) for b in dn_bids)
                    up_cross = 1.0 - dn_best_bid
                    print(f"  UP cross (1 - DN best bid {dn_best_bid}): {up_cross:.4f} = {round(up_cross*100)}¢")

            # Final cross-book price (same as Polymarket UI)
            up_candidates = [p for p in [up_direct, up_cross] if p and 0.01 < p < 0.99]
            dn_candidates = [p for p in [dn_direct, dn_cross] if p and 0.01 < p < 0.99]
            up_final = min(up_candidates) if up_candidates else None
            dn_final = min(dn_candidates) if dn_candidates else None
            print(f"\n  >>> CROSS-BOOK RESULT: UP={round(up_final*100) if up_final else '?'}¢  DOWN={round(dn_final*100) if dn_final else '?'}¢ <<<")
            print(f"  (This should match Polymarket's Buy buttons exactly)")
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
