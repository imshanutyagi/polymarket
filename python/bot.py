"""
Polymarket BTC Binary Options Trading Bot — Fast Edition
Single-file, pure Python, direct WebSocket, instant execution.
Usage: python3 bot.py
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta

import httpx
import websockets
from dotenv import load_dotenv

# Load .env from same directory as this script
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ═══ CONFIG ═══════════════════════════════════════════════════════════════════

TRADE_SIZE_DOLLARS = 8.0        # $ per trade
MAX_ENTRY_PRICE = 55            # max cents to pay per share
PROFIT_TARGET = 4.0             # $ per cycle target
STOP_LOSS = 1.50                # $ max loss per trade
MAX_TRADES = 8                  # max trades per cycle
PROFIT_MODE = "smart"           # "quick"=$0.50, "patient"=$1.00, "smart"=time-adjusted
OBSERVE_SECONDS = 60            # observe before first trade
ANTI_WHIPSAW = 15.0             # seconds between entries
MIN_CONFIDENCE = 30             # minimum signal confidence to enter

# Polymarket
POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
BINANCE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

# Credentials
PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
PROXY_ADDRESS = os.getenv("POLY_PROXY_ADDRESS", "")

# ═══ CLOB CLIENT SETUP ═══════════════════════════════════════════════════════

clob_client = None
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, CreateOrderOptions, BalanceAllowanceParams, AssetType
    from py_clob_client.constants import POLYGON
    from py_clob_client.order_builder.constants import BUY, SELL

    if PRIVATE_KEY and PROXY_ADDRESS:
        c = ClobClient(CLOB_URL, key=PRIVATE_KEY, chain_id=POLYGON, signature_type=2, funder=PROXY_ADDRESS)
        creds = c.create_or_derive_api_creds()
        c.set_api_creds(creds)
        clob_client = c
        print(f"[INIT] CLOB client ready (key={creds.api_key[:8]}...)")
    else:
        print("[INIT] Missing POLY_PRIVATE_KEY or POLY_PROXY_ADDRESS — live trading disabled")
except Exception as e:
    print(f"[INIT] CLOB client failed: {e}")


# ═══ MARKET STATE ═════════════════════════════════════════════════════════════

class Market:
    def __init__(self):
        self.slug = ""
        self.up_token = ""
        self.dn_token = ""
        self.up_price = 50          # cents
        self.dn_price = 50          # cents
        self.btc_price = 0.0
        self.price_to_beat = 0.0
        self.cycle_end_time = 0.0
        self.prices_loaded = False

    def time_left(self) -> float:
        return max(0, self.cycle_end_time - time.time())

market = Market()


# ═══ PORTFOLIO ════════════════════════════════════════════════════════════════

class Portfolio:
    def __init__(self):
        self.balance = 0.0
        self.cycle_profit = 0.0

    def sync_balance(self):
        """Query actual USDC balance from CLOB."""
        if not clob_client:
            return
        try:
            data = clob_client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
            )
            self.balance = float(data.get("balance", "0")) / 1e6
        except Exception as e:
            print(f"[BALANCE] Sync failed: {e}")

portfolio = Portfolio()


# ═══ STRATEGY (NCAIO core logic) ═════════════════════════════════════════════

class Strategy:
    EMA_FAST_ALPHA = 2.0 / 16.0
    EMA_SLOW_ALPHA = 2.0 / 46.0
    RSI_PERIOD = 14

    def __init__(self):
        self.reset()

    def reset(self):
        self.ema_fast = None
        self.ema_slow = None
        self.rsi_prices = []
        self.price_history = []     # [(time, up, dn, btc), ...]
        self.cycle_start = 0.0
        self.last_entry_time = 0.0
        self.last_flip_time = 0.0
        self.trade_count = 0
        self.cycle_pnl = 0.0
        self.consecutive_losses = 0
        self.active_trade = None    # {side, shares, entry_price, entry_time, peak_pnl}
        self.confidence = 0
        self.phase = "observing"

    def update(self, up: int, dn: int, btc: float):
        """Called on every price update."""
        now = time.time()
        if self.cycle_start == 0.0:
            self.cycle_start = now

        # History (20 min buffer)
        self.price_history.append((now, float(up), float(dn), btc))
        cutoff = now - 1200
        self.price_history = [h for h in self.price_history if h[0] >= cutoff]

        # EMA
        p = float(up)
        if self.ema_fast is None:
            self.ema_fast = self.ema_slow = p
        else:
            self.ema_fast += self.EMA_FAST_ALPHA * (p - self.ema_fast)
            self.ema_slow += self.EMA_SLOW_ALPHA * (p - self.ema_slow)

        # RSI buffer
        self.rsi_prices.append(p)
        if len(self.rsi_prices) > 100:
            self.rsi_prices = self.rsi_prices[-100:]

    def velocity(self, window: float) -> float:
        now = time.time()
        pts = [(t, p) for t, p, _, _ in self.price_history if t >= now - window]
        if len(pts) < 2:
            return 0.0
        dt = pts[-1][0] - pts[0][0]
        return (pts[-1][1] - pts[0][1]) / dt if dt > 1 else 0.0

    def rsi(self) -> float:
        if len(self.rsi_prices) < self.RSI_PERIOD + 1:
            return 50.0
        changes = [self.rsi_prices[i] - self.rsi_prices[i-1]
                   for i in range(len(self.rsi_prices) - self.RSI_PERIOD, len(self.rsi_prices))]
        gains = [c for c in changes if c > 0]
        losses = [-c for c in changes if c < 0]
        avg_gain = sum(gains) / self.RSI_PERIOD if gains else 0.001
        avg_loss = sum(losses) / self.RSI_PERIOD if losses else 0.001
        return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    def is_choppy(self) -> bool:
        now = time.time()
        pts = [(t, p) for t, p, _, _ in self.price_history if t >= now - 120]
        if len(pts) < 5:
            return False
        reversals = sum(1 for i in range(2, len(pts))
                       if (pts[i-1][1] - pts[i-2][1]) * (pts[i][1] - pts[i-1][1]) < 0)
        return reversals >= 6 and abs(pts[-1][1] - pts[0][1]) < 2.0

    def min_movement(self) -> bool:
        now = time.time()
        prices = [p for t, p, _, _ in self.price_history if t >= now - 120]
        if len(prices) < 3:
            return False
        return (max(prices) - min(prices)) >= 2.0

    def compute_signal(self, up: int, dn: int, btc: float, target: float, time_left: float):
        """Returns (side, confidence, reason)."""
        score = 0.0
        reasons = []

        # Signal 1: BTC vs target (weight 40)
        if btc > 0 and target > 0:
            delta = btc - target
            elapsed = 1.0 - min(1.0, time_left / 3600.0)
            factor = 0.8 + elapsed * 1.2
            sig = max(-40, min(40, (delta / 10.0) * factor))
            if abs(sig) > 5:
                reasons.append(f"BTC ${abs(delta):.0f} {'above' if delta > 0 else 'below'}")
            score += sig

        # Signal 2: EMA crossover (weight 20)
        if self.ema_fast and self.ema_slow:
            diff = self.ema_fast - self.ema_slow
            if abs(diff) > 0.3:
                score += max(-20, min(20, diff * 20.0))
                reasons.append(f"EMA {'bull' if diff > 0 else 'bear'}")

        # Signal 3: Velocity (weight 15)
        v2 = self.velocity(120)
        v5 = self.velocity(300)
        if abs(v2) > 0.1:
            vs = max(-15, min(15, v2 * 50.0))
            vs *= 1.2 if v5 * v2 > 0 else 0.6
            score += max(-15, min(15, vs))
            reasons.append(f"vel {v2:+.2f}")

        # Signal 4: RSI (weight 10)
        r = self.rsi()
        if r > 70:
            score -= min(10, (r - 70) * 0.33)
        elif r < 30:
            score += min(10, (30 - r) * 0.33)
        else:
            score += (r - 50) * 0.1

        # Signal 5: Entry zone (weight 15)
        ep = up if score > 0 else dn if score < 0 else min(up, dn)
        zs = 15.0 if 30 <= ep <= 50 else 8.0 if ep <= 60 else -5.0 if ep > 60 else 3.0
        if score < 0:
            zs = -zs
        score += zs

        side = "up" if score > 0 else "down" if score < 0 else "none"
        conf = min(100, int(abs(score)))
        self.confidence = conf
        return side, conf, " | ".join(reasons[:3]) or "weak"

    def determine_phase(self, time_left: float) -> str:
        now = time.time()
        if self.cycle_start > 0 and (now - self.cycle_start) < OBSERVE_SECONDS:
            return "observing"
        if time_left < 300:
            return "exit_only"
        if self.consecutive_losses >= 2 or time_left < 600:
            return "cautious"
        if self.cycle_pnl <= -6.0:
            return "exit_only"
        return "active"

    def check_exit(self, up: int, dn: int, time_left: float):
        """Returns (should_exit, reason) for active trade."""
        t = self.active_trade
        if not t:
            return False, ""

        now = time.time()
        price = up if t["side"] == "up" else dn
        pnl = t["shares"] * (price - t["entry_price"]) / 100.0
        hold = now - t["entry_time"]
        t["peak_pnl"] = max(t.get("peak_pnl", 0), pnl)

        # Time stop
        if time_left < 120:
            return True, f"time-stop (${pnl:+.2f})"

        # Phase exit
        self.phase = self.determine_phase(time_left)
        if self.phase == "exit_only":
            return True, f"phase-exit (${pnl:+.2f})"

        # Grace cap
        if hold <= 180 and pnl <= -2.50:
            return True, f"grace-cap ${pnl:.2f}"

        # Stop loss (after 3 min)
        if hold > 180 and pnl <= -STOP_LOSS:
            return True, f"stop-loss ${pnl:.2f}"

        # Smart profit
        if PROFIT_MODE == "smart":
            target = 0.50 if time_left <= 300 else 0.60 if time_left <= 600 else 0.80 if time_left <= 1800 else 1.00
            if target <= pnl <= 1.50:
                return True, f"smart +${pnl:.2f}"
            peak = t.get("peak_pnl", 0)
            if peak > 1.50 and pnl < peak - 0.15:
                return True, f"trail +${pnl:.2f} (peak ${peak:.2f})"
        elif PROFIT_MODE == "quick" and pnl >= 0.50:
            return True, f"quick +${pnl:.2f}"

        # Timeout
        if hold > 8 * 60 and 0.10 <= pnl < 0.80:
            return True, f"timeout +${pnl:.2f}"

        return False, ""

    def check_entry(self, side: str, conf: int, up: int, dn: int, time_left: float):
        """Returns (should_enter, entry_price, shares) or (False, 0, 0)."""
        if side == "none" or self.active_trade:
            return False, 0, 0
        if self.trade_count >= MAX_TRADES:
            return False, 0, 0
        if self.cycle_pnl >= PROFIT_TARGET:
            return False, 0, 0
        if self.cycle_pnl <= -6.0 or time_left < 300:
            return False, 0, 0

        now = time.time()
        if (now - self.last_entry_time) < ANTI_WHIPSAW:
            return False, 0, 0

        # Confidence threshold
        min_conf = MIN_CONFIDENCE
        if self.phase == "cautious":
            min_conf = max(min_conf, 75)
        if self.cycle_pnl < 0:
            min_conf = max(min_conf, 60)
        if self.consecutive_losses >= 2:
            min_conf = max(min_conf, 75)
        if conf < min_conf:
            return False, 0, 0

        price = up if side == "up" else dn
        if price <= 0 or price > MAX_ENTRY_PRICE:
            return False, 0, 0
        if self.is_choppy() or not self.min_movement():
            return False, 0, 0

        # Momentum against
        v2 = self.velocity(120)
        if (side == "up" and v2 < -0.25) or (side == "down" and v2 > 0.25):
            return False, 0, 0

        # Spike filter
        v30 = self.velocity(30)
        v10 = self.velocity(10)
        if abs(v30) > 0.6:
            if (side == "up" and v30 > 0.6 and v10 > 0) or (side == "down" and v30 < -0.6 and v10 < 0):
                return False, 0, 0

        # Sizing
        dollars = TRADE_SIZE_DOLLARS
        if time_left < 900:
            dollars = min(dollars, 5.0)
        shares = dollars / (price / 100.0)
        cost = shares * (price / 100.0)
        if portfolio.balance < cost:
            shares = portfolio.balance / (price / 100.0)
        if shares <= 0:
            return False, 0, 0

        return True, price, shares

strategy = Strategy()


# ═══ ORDER EXECUTION ══════════════════════════════════════════════════════════

async def place_buy(side: str, shares: float, price_cents: int) -> bool:
    if not clob_client:
        print(f"[ORDER] No CLOB client — BUY {side} skipped")
        return False
    token = market.up_token if side == "up" else market.dn_token
    if not token:
        return False
    buy_price = round(min(0.97, (price_cents + 2) / 100.0), 2)
    size = max(1.0, float(int(shares)))
    try:
        args = OrderArgs(price=buy_price, size=size, side=BUY, token_id=token)
        opts = CreateOrderOptions(neg_risk=False, tick_size="0.01")
        signed = await asyncio.to_thread(clob_client.create_order, args, opts)
        resp = await asyncio.to_thread(clob_client.post_order, signed, OrderType.FOK)
        success = resp.get("success", False) if isinstance(resp, dict) else False
        if success:
            print(f"[ORDER] ✅ BUY {size} {side.upper()} @ {int(buy_price*100)}¢ — matched")
        else:
            print(f"[ORDER] ⚠️ BUY response: {resp}")
        return success
    except Exception as e:
        print(f"[ORDER] ❌ BUY failed: {str(e)[:150]}")
        return False

async def place_sell(side: str, shares: float, price_cents: int) -> bool:
    if not clob_client:
        print(f"[ORDER] No CLOB client — SELL {side} skipped")
        return False
    token = market.up_token if side == "up" else market.dn_token
    if not token:
        return False

    # Query actual shares on CLOB
    actual = shares
    try:
        data = await asyncio.to_thread(
            clob_client.get_balance_allowance,
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, signature_type=2, token_id=token)
        )
        clob_shares = float(data.get("balance", "0")) / 1e6
        if clob_shares > 0:
            actual = clob_shares
            print(f"[ORDER] Actual CLOB shares: {actual:.2f} (strategy: {shares:.2f})")
    except Exception:
        pass

    sell_price = round(max(0.01, (price_cents - 2) / 100.0), 2)
    size = round(actual * 0.99, 2)
    if size < 0.1:
        return False

    # Try FOK first, then GTC
    for order_type, label in [(OrderType.FOK, "FOK"), (OrderType.GTC, "GTC")]:
        try:
            p = sell_price if label == "FOK" else round(max(0.01, (price_cents - 5) / 100.0), 2)
            args = OrderArgs(price=p, size=size, side=SELL, token_id=token)
            opts = CreateOrderOptions(neg_risk=False, tick_size="0.01")
            signed = await asyncio.to_thread(clob_client.create_order, args, opts)
            resp = await asyncio.to_thread(clob_client.post_order, signed, order_type)
            success = resp.get("success", False) if isinstance(resp, dict) else False
            if success:
                print(f"[ORDER] ✅ SELL {size} {side.upper()} @ {int(p*100)}¢ ({label}) — matched")
                return True
            print(f"[ORDER] {label} sell response: {resp}")
        except Exception as e:
            print(f"[ORDER] {label} sell failed: {str(e)[:150]}")
    return False


# ═══ MARKET DISCOVERY ═════════════════════════════════════════════════════════

def get_et_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc) + timedelta(hours=-4)

def make_slug(et_time):
    h = et_time.hour
    ampm = "pm" if h >= 12 else "am"
    h12 = h % 12 or 12
    return f"bitcoin-up-or-down-{et_time.strftime('%B').lower()}-{et_time.day}-{et_time.year}-{h12}{ampm}-et"

async def discover_market():
    """Find the current active hourly BTC market."""
    et_now = get_et_now()
    slug = make_slug(et_now)

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{GAMMA_URL}/events?slug={slug}")
        data = resp.json() if resp.status_code == 200 else []

    if not data or len(data) == 0:
        return False

    event = data[0]
    if event.get("closed"):
        return False

    for m in event.get("markets", []):
        if not m.get("closed") and m.get("active"):
            outcomes = json.loads(m.get("outcomes", "[]"))
            tokens = json.loads(m.get("clobTokenIds", "[]"))
            prices = json.loads(m.get("outcomePrices", "[]"))

            yes_i = next((i for i, o in enumerate(outcomes) if o.lower() in ("yes", "up", "above")), 0)
            no_i = next((i for i, o in enumerate(outcomes) if o.lower() in ("no", "down", "below")), 1)

            if len(tokens) < 2:
                continue

            market.slug = slug
            market.up_token = tokens[yes_i]
            market.dn_token = tokens[no_i]
            market.up_price = max(1, min(99, round(float(prices[yes_i]) * 100)))
            market.dn_price = max(1, min(99, round(float(prices[no_i]) * 100)))

            # End time = slug hour + 1, in ET
            end_hour = et_now.hour + 1
            end_et = et_now.replace(hour=0, minute=0, second=0, microsecond=0).replace(hour=end_hour % 24)
            if end_hour >= 24:
                end_et += timedelta(days=1)
            market.cycle_end_time = end_et.timestamp()

            # Extract target price from title
            title = m.get("question", "") or event.get("title", "")
            try:
                import re
                match = re.search(r'\$([0-9,]+\.?\d*)', title)
                if match:
                    market.price_to_beat = float(match.group(1).replace(",", ""))
            except Exception:
                pass

            market.prices_loaded = True
            return True
    return False


# ═══ BTC PRICE ════════════════════════════════════════════════════════════════

async def fetch_btc_price():
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(BINANCE_URL)
            if resp.status_code == 200:
                market.btc_price = float(resp.json()["price"])
    except Exception:
        pass


# ═══ MAIN LOOP ════════════════════════════════════════════════════════════════

async def run_bot():
    print("=" * 60)
    print("  POLYMARKET BTC TRADING BOT — FAST EDITION")
    print("=" * 60)

    while True:
        # 1. Discover market
        print("\n[BOT] Searching for active market...")
        while not await discover_market():
            print("[BOT] No market found, retrying in 5s...")
            await asyncio.sleep(5)

        tl = market.time_left()
        print(f"[BOT] Connected: {market.slug}")
        print(f"[BOT] UP={market.up_price}¢ DN={market.dn_price}¢ | {int(tl/60)}min left")

        # 2. Sync balance
        await asyncio.to_thread(portfolio.sync_balance)
        print(f"[BOT] Balance: ${portfolio.balance:.2f}")

        # 3. Reset strategy
        strategy.reset()
        portfolio.cycle_profit = 0.0

        # 4. Fetch BTC price
        await fetch_btc_price()
        print(f"[BOT] BTC: ${market.btc_price:.0f} | Target: ${market.price_to_beat:.0f}")

        # 5. Stream prices and trade
        try:
            await trade_cycle()
        except Exception as e:
            print(f"[BOT] Cycle error: {e}")

        # 6. Cycle ended — close any remaining position
        if strategy.active_trade:
            print(f"[BOT] Cycle end — force closing position")
            side = strategy.active_trade["side"]
            shares = strategy.active_trade["shares"]
            price = market.up_price if side == "up" else market.dn_price
            await place_sell(side, shares, price)
            strategy.active_trade = None

        print(f"\n[BOT] Cycle complete | P&L: ${strategy.cycle_pnl:+.2f} | Trades: {strategy.trade_count}")
        print(f"[BOT] Waiting for next cycle...")
        await asyncio.sleep(5)


async def trade_cycle():
    """Connect to Polymarket WebSocket and trade until cycle ends."""
    btc_fetch_time = 0

    async with websockets.connect(POLY_WS_URL, ping_interval=None, close_timeout=5) as ws:
        # Subscribe
        await ws.send(json.dumps({
            "assets_ids": [market.up_token, market.dn_token],
            "type": "market",
            "custom_feature_enabled": True
        }))
        print(f"[WS] Connected — streaming prices")

        # Heartbeat
        async def heartbeat():
            while True:
                await asyncio.sleep(10)
                try:
                    await ws.send("PING")
                except Exception:
                    break
        hb = asyncio.create_task(heartbeat())

        # BTC price updater (every 30s)
        async def btc_updater():
            while True:
                await fetch_btc_price()
                await asyncio.sleep(30)
        btc_task = asyncio.create_task(btc_updater())

        # Balance sync (every 60s)
        async def balance_syncer():
            while True:
                await asyncio.sleep(60)
                await asyncio.to_thread(portfolio.sync_balance)
        bal_task = asyncio.create_task(balance_syncer())

        best = {"up_ask": None, "up_bid": None, "dn_ask": None, "dn_bid": None}
        last_log = 0

        try:
            async for message in ws:
                # Check cycle end
                tl = market.time_left()
                if tl <= 0:
                    print("[WS] Cycle ended")
                    break

                if message == "PONG":
                    continue

                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue

                # Parse prices from WS events
                updated = False

                if isinstance(data, list):
                    # Initial book snapshots
                    for item in data:
                        if not isinstance(item, dict):
                            continue
                        aid = item.get("asset_id", "")
                        asks = item.get("asks", [])
                        bids = item.get("bids", [])
                        if aid == market.up_token:
                            if asks: best["up_ask"] = min(float(a["price"]) for a in asks)
                            if bids: best["up_bid"] = max(float(b["price"]) for b in bids)
                        elif aid == market.dn_token:
                            if asks: best["dn_ask"] = min(float(a["price"]) for a in asks)
                            if bids: best["dn_bid"] = max(float(b["price"]) for b in bids)
                    updated = True

                elif isinstance(data, dict):
                    event = data.get("event_type", "")
                    aid = data.get("asset_id", "")

                    if event == "best_bid_ask":
                        if aid == market.up_token:
                            if data.get("best_ask"): best["up_ask"] = float(data["best_ask"])
                            if data.get("best_bid"): best["up_bid"] = float(data["best_bid"])
                        elif aid == market.dn_token:
                            if data.get("best_ask"): best["dn_ask"] = float(data["best_ask"])
                            if data.get("best_bid"): best["dn_bid"] = float(data["best_bid"])
                        updated = True

                    elif event == "price_change":
                        for pc in data.get("price_changes", []):
                            pa = pc.get("asset_id", "")
                            if pa == market.up_token:
                                if pc.get("best_ask"): best["up_ask"] = float(pc["best_ask"])
                                if pc.get("best_bid"): best["up_bid"] = float(pc["best_bid"])
                            elif pa == market.dn_token:
                                if pc.get("best_ask"): best["dn_ask"] = float(pc["best_ask"])
                                if pc.get("best_bid"): best["dn_bid"] = float(pc["best_bid"])
                        updated = True

                if not updated:
                    continue

                # Cross-book pricing
                up_direct = best["up_ask"]
                dn_direct = best["dn_ask"]
                up_cross = (1.0 - best["dn_bid"]) if best["dn_bid"] else None
                dn_cross = (1.0 - best["up_bid"]) if best["up_bid"] else None

                up_cands = [p for p in [up_direct, up_cross] if p and 0.01 < p < 0.99]
                dn_cands = [p for p in [dn_direct, dn_cross] if p and 0.01 < p < 0.99]

                if up_cands and dn_cands:
                    market.up_price = max(1, min(99, round(min(up_cands) * 100)))
                    market.dn_price = max(1, min(99, round(min(dn_cands) * 100)))

                # Update strategy indicators
                strategy.update(market.up_price, market.dn_price, market.btc_price)
                strategy.phase = strategy.determine_phase(tl)

                # Log every 10s
                now = time.time()
                if now - last_log >= 10:
                    trade_status = f"HOLDING {strategy.active_trade['side'].upper()}" if strategy.active_trade else "NO POSITION"
                    print(f"[BOT] UP={market.up_price}¢ DN={market.dn_price}¢ | conf={strategy.confidence} | "
                          f"phase={strategy.phase} | {trade_status} | ${portfolio.balance:.2f} | "
                          f"trades={strategy.trade_count}/{MAX_TRADES} | pnl=${strategy.cycle_pnl:+.2f} | "
                          f"{int(tl/60)}min left")
                    last_log = now

                # === TRADING LOGIC ===

                # Check exit first
                should_exit, reason = strategy.check_exit(market.up_price, market.dn_price, tl)
                if should_exit and strategy.active_trade:
                    t = strategy.active_trade
                    price = market.up_price if t["side"] == "up" else market.dn_price
                    pnl = t["shares"] * (price - t["entry_price"]) / 100.0

                    print(f"[TRADE] EXIT {t['side'].upper()} | {reason}")
                    sold = await place_sell(t["side"], t["shares"], price)
                    if sold:
                        strategy.cycle_pnl += pnl
                        strategy.trade_count += 1
                        if pnl < 0:
                            strategy.consecutive_losses += 1
                        else:
                            strategy.consecutive_losses = 0
                        portfolio.cycle_profit += pnl
                        print(f"[TRADE] PnL: ${pnl:+.2f} | Cycle: ${strategy.cycle_pnl:+.2f}")
                    else:
                        print(f"[TRADE] ⚠️ Sell failed — position may still be open on CLOB")
                    strategy.active_trade = None
                    await asyncio.to_thread(portfolio.sync_balance)

                # Check entry
                if not strategy.active_trade and strategy.phase in ("active", "cautious"):
                    side, conf, reason = strategy.compute_signal(
                        market.up_price, market.dn_price, market.btc_price, market.price_to_beat, tl
                    )
                    enter, price, shares = strategy.check_entry(side, conf, market.up_price, market.dn_price, tl)
                    if enter:
                        print(f"[TRADE] ENTRY {side.upper()} | {shares:.1f}sh @ {price}¢ | conf={conf} | {reason}")
                        bought = await place_buy(side, shares, price)
                        if bought:
                            strategy.active_trade = {
                                "side": side, "shares": shares, "entry_price": price,
                                "entry_time": time.time(), "peak_pnl": 0.0
                            }
                            strategy.last_entry_time = time.time()
                            await asyncio.to_thread(portfolio.sync_balance)
                        else:
                            print(f"[TRADE] ⚠️ Buy failed — skipping entry")

        finally:
            hb.cancel()
            btc_task.cancel()
            bal_task.cancel()


# ═══ ENTRY POINT ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    asyncio.run(run_bot())
