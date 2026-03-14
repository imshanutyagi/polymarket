import asyncio
import json
import math
import os
import time
import traceback
from datetime import datetime
import httpx
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Dict
from strategies import HighFrequencyScalper, TrendScaler, ClaudeStrategy

# --- Live Trading Setup ---
from dotenv import load_dotenv
import os
# Ensure it always finds the .env file exactly where main.py is, regardless of where uvicorn is launched
dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path)

LIVE_TRADING_AVAILABLE = False
clob_client = None
clob_clients = {}
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds
    from py_clob_client.constants import POLYGON
    from py_clob_client.order_builder.constants import BUY, SELL

    api_key = os.getenv("POLY_API_KEY", "")
    api_secret = os.getenv("POLY_API_SECRET", "")
    api_passphrase = os.getenv("POLY_API_PASSPHRASE", "")
    proxy_address = os.getenv("POLY_PROXY_ADDRESS", "")
    private_key = os.getenv("POLY_PRIVATE_KEY", "")

    if private_key and proxy_address:
        # Build clients for each signature type — we'll try them in order when placing orders
        clob_clients = {}
        for sig_type, funder in [(0, None), (1, proxy_address), (2, proxy_address)]:
            try:
                kwargs = {"key": private_key, "chain_id": POLYGON, "signature_type": sig_type}
                if funder:
                    kwargs["funder"] = funder
                c = ClobClient("https://clob.polymarket.com", **kwargs)
                creds = c.create_or_derive_api_creds()
                c.set_api_creds(creds)
                clob_clients[sig_type] = c
            except Exception:
                pass
        # Use type=0 (EOA) as default — works when private key IS the trading account
        clob_client = clob_clients.get(0) or clob_clients.get(1) or clob_clients.get(2)
        if clob_client:
            LIVE_TRADING_AVAILABLE = True
            print(f"[LIVE] CLOB credentials derived successfully! key={creds.api_key[:8]}...")
    else:
        print("[LIVE] POLY_PRIVATE_KEY or POLY_PROXY_ADDRESS not set — Live trading disabled.")
except ImportError:
    print("[LIVE] py-clob-client not installed — Live trading disabled. Run: pip install py-clob-client")
except Exception as e:
    print(f"[LIVE] Failed to initialize CLOB client: {e}")

scalper_4 = HighFrequencyScalper()
scalper_5 = HighFrequencyScalper()
scalper_6 = TrendScaler()
claude_strategy = ClaudeStrategy()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/proxy/clob/{token_id}")
async def proxy_clob(token_id: str):
    try:
        async with httpx.AsyncClient() as client:
            # Call Polymarket CLOB API directly (no third-party proxy needed from backend)
            resp = await client.get(
                f"https://clob.polymarket.com/book?token_id={token_id}",
                timeout=10.0
            )
            return resp.json()
    except Exception as e:
        return {"error": str(e), "asks": [], "bids": []}

@app.get("/proxy/gamma/")
async def proxy_gamma(slug: str):
    try:
        async with httpx.AsyncClient() as client:
            # Call Polymarket Gamma API directly (no third-party proxy needed from backend)
            resp = await client.get(
                f"https://gamma-api.polymarket.com/events?slug={slug}",
                timeout=10.0
            )
            return resp.json()
    except Exception as e:
        return []


@app.post("/api/backtest")
async def run_backtest_endpoint(body: dict = {}):
    """Run a Monte Carlo paper-trade simulation for all strategies starting from $10,000."""
    import random

    cycles    = max(1, min(20, int(body.get("cycles", 10))))
    vol       = float(body.get("volatility", 0.010))
    TICK      = 5        # seconds per simulated tick
    DUR       = 3600     # 1-hour cycle
    START_BAL = 10000.0
    TAKE_PROFIT = 1.0    # cash out per trade at $1+
    CYCLE_CAP   = 4.0    # stop new entries after $4 cycle profit

    labels = {
        "strategy_a": "Smart Balancer", "strategy_b": "Momentum Sniper",
        "strategy_c": "Fixed Target",   "strategy_c_trailing": "C+Trailing",
        "strategy_d": "Strategy D",     "strategy_e": "Strategy E",
        "strategy_f": "Strategy F",     "strategy_7": "Strategy 7",
        "strategy_cpt": "CPT",          "strategy_claude": "Claude AI",
    }

    totals  = {k: 0.0 for k in labels}
    t_count = {k: 0   for k in labels}
    t_wins  = {k: 0   for k in labels}

    def get_pnl(p: Portfolio, up: int, dn: int) -> float:
        uv = p.positions["up"] * (up / 100.0)
        dv = p.positions["down"] * (dn / 100.0)
        return (uv + dv) - p.total_spent

    def micro_trend(history_sim, t_sim, window=60):
        h = [h for h in history_sim if h[0] >= t_sim - window]
        if len(h) < 4: return "neutral"
        chg = (h[-1][1] - h[0][1]) / h[0][1]
        if chg > 0.0002: return "up"
        if chg < -0.0002: return "down"
        return "neutral"

    def record(key, p, start_bal, up_wins):
        p.reset_for_cycle(up_wins, not up_wins)
        profit = p.balance - start_bal
        totals[key] += profit
        t_count[key] += 1
        if profit > 0: t_wins[key] += 1

    for _ in range(cycles):
        start_price = random.uniform(92, 108)
        ticks = []
        price = start_price
        history_sim: list = []
        for t in range(0, DUR + TICK, TICK):
            price *= (1 + random.gauss(0, vol * math.sqrt(TICK / DUR)))
            price  = max(50, min(150, price))
            tl     = max(0, DUR - t)
            tf     = max(0.001, tl / DUR)
            z      = ((price - start_price) / start_price) / (vol * math.sqrt(tf))
            raw_up = 0.5 * (1 + math.erf(z / math.sqrt(2)))
            up_c   = max(1, min(99, round(raw_up * 100)))
            dn_c   = 100 - up_c
            up_p   = min(99, up_c + 1)
            dn_p   = min(99, dn_c + 1)
            history_sim.append((t, price))
            ticks.append({"t": t, "tl": tl, "price": price, "up": up_p, "dn": dn_p})

        up_wins = ticks[-1]["price"] > start_price

        def new_port(key): p = Portfolio(); p.balance = START_BAL + totals[key]; return p

        # ── Strategy A – Smart Balancer ──────────────────────────────────────
        pA = new_port("strategy_a")
        for tk in ticks:
            up, dn, tl = tk["up"], tk["dn"], tk["tl"]
            if tl <= 0: break
            # Take-profit: cash out at $1+ and re-enter if cycle cap not hit
            if pA.total_spent > 0:
                pnl = get_pnl(pA, up, dn)
                if pnl >= TAKE_PROFIT:
                    pA.cash_out(up, dn); continue
            if pA.cycle_profit >= CYCLE_CAP: continue
            u = pA.positions["up"]; d = pA.positions["down"]
            au = pA.spent["up"] / u if u > 0 else 0
            TARGET = 30.0
            if u < TARGET:
                sh = TARGET - u
                if u > 0:
                    if dn <= 96 - au: pA.buy("up", sh, up)
                elif up <= 15 or 85 <= up <= 90: pA.buy("up", sh, up)
            if d < TARGET:
                sh = TARGET - d
                if u > 0:
                    if dn <= 96 - au: pA.buy("down", sh, dn)
                elif dn <= 15 or 85 <= dn <= 90: pA.buy("down", sh, dn)
        record("strategy_a", pA, START_BAL + totals["strategy_a"] - (pA.balance - (START_BAL + totals["strategy_a"])), up_wins)
        # simpler: just track directly
        totals["strategy_a"] = round(pA.balance - START_BAL, 4)
        t_count["strategy_a"] = t_count["strategy_a"]  # already counted in record? no — redo simply:

        # Re-implement record inline for cleaner tracking:

        # Reset and redo all strategies with clean inline tracking
        pass

    # --- Clean re-implementation ---
    totals  = {k: 0.0 for k in labels}
    t_count = {k: 0   for k in labels}
    t_wins  = {k: 0   for k in labels}

    for _ in range(cycles):
        start_price = random.uniform(92, 108)
        ticks = []
        price = start_price
        history_sim: list = []
        for t in range(0, DUR + TICK, TICK):
            price *= (1 + random.gauss(0, vol * math.sqrt(TICK / DUR)))
            price  = max(50, min(150, price))
            tl     = max(0, DUR - t)
            tf     = max(0.001, tl / DUR)
            z      = ((price - start_price) / start_price) / (vol * math.sqrt(tf))
            raw_up = 0.5 * (1 + math.erf(z / math.sqrt(2)))
            up_c   = max(1, min(99, round(raw_up * 100)))
            dn_c   = 100 - up_c
            ticks.append({"t": t, "tl": tl, "price": price,
                           "up": min(99, up_c + 1), "dn": min(99, dn_c + 1)})
            history_sim.append((t, price))

        up_wins = ticks[-1]["price"] > start_price

        def sim(key):
            p = Portfolio(); p.balance = START_BAL + totals[key]; return p

        def finish(key, p):
            p.reset_for_cycle(up_wins, not up_wins)
            profit = round(p.balance - (START_BAL + totals[key]), 4)
            totals[key]  = round(totals[key] + profit, 4)
            t_count[key] += 1
            if profit > 0: t_wins[key] += 1

        def pnl(p, up, dn):
            return (p.positions["up"]*(up/100) + p.positions["down"]*(dn/100)) - p.total_spent

        def mt(t_sim, window=60):
            h = [h for h in history_sim if h[0] >= t_sim - window]
            if len(h) < 4: return "neutral"
            c = (h[-1][1] - h[0][1]) / h[0][1]
            return "up" if c > 0.0002 else ("down" if c < -0.0002 else "neutral")

        # ── Strategy A – Smart Balancer ──────────────────────────────────────
        pA = sim("strategy_a")
        for tk in ticks:
            up, dn, tl = tk["up"], tk["dn"], tk["tl"]
            if tl <= 0: break
            if pA.total_spent > 0 and pnl(pA, up, dn) >= TAKE_PROFIT:
                pA.cash_out(up, dn); continue
            if pA.cycle_profit >= CYCLE_CAP: continue
            u = pA.positions["up"]; d = pA.positions["down"]
            au = pA.spent["up"] / u if u > 0 else 0
            if u < 30:
                if u > 0:
                    if up <= 96 - au: pA.buy("up", 30 - u, up)
                elif up <= 15 or 85 <= up <= 90: pA.buy("up", 30 - u, up)
            if d < 30:
                if u > 0:
                    if dn <= 96 - au: pA.buy("down", 30 - d, dn)
                elif dn <= 15 or 85 <= dn <= 90: pA.buy("down", 30 - d, dn)
        finish("strategy_a", pA)

        # ── Strategy B – Momentum Sniper ─────────────────────────────────────
        pB = sim("strategy_b"); b_done = False
        for tk in ticks:
            up, dn, tl = tk["up"], tk["dn"], tk["tl"]
            if tl <= 30: break
            if not b_done and pB.cycle_profit < CYCLE_CAP:
                h = [h for h in history_sim if h[0] >= tk["t"] - 60]
                if len(h) > 10:
                    pct = (h[-1][1] - h[0][1]) / h[0][1]
                    if pct >= 0.0002 and up < 80:
                        if pB.buy("up", 15.0, up): b_done = True
                    elif pct <= -0.0002 and dn < 80:
                        if pB.buy("down", 15.0, dn): b_done = True
            elif b_done:
                us = pB.spent["up"]; ds = pB.spent["down"]
                if us > 0:
                    v = pB.positions["up"]*(up/100); p2 = v - us
                    if p2 >= TAKE_PROFIT or (v/us) >= 1.30 or (v/us) <= 0.60:
                        pB.cash_out(up, dn); b_done = False
                elif ds > 0:
                    v = pB.positions["down"]*(dn/100); p2 = v - ds
                    if p2 >= TAKE_PROFIT or (v/ds) >= 1.30 or (v/ds) <= 0.60:
                        pB.cash_out(up, dn); b_done = False
        finish("strategy_b", pB)

        # ── Strategy C – Fixed Target (both sides ≤45c) ──────────────────────
        pC = sim("strategy_c")
        for tk in ticks:
            up, dn, tl = tk["up"], tk["dn"], tk["tl"]
            if tl <= 60: break
            if pC.total_spent > 0 and pnl(pC, up, dn) >= TAKE_PROFIT:
                pC.cash_out(up, dn); continue
            if pC.cycle_profit >= CYCLE_CAP: continue
            u = pC.positions["up"]; d = pC.positions["down"]
            if u < 30 and up <= 45: pC.buy("up",  30 - u, up)
            if d < 30 and dn <= 45: pC.buy("down", 30 - d, dn)
        finish("strategy_c", pC)

        # ── Strategy 7 – Mean Reversion ──────────────────────────────────────
        p7 = sim("strategy_7")
        for tk in ticks:
            up, dn, tl = tk["up"], tk["dn"], tk["tl"]
            if tl <= 10: break
            if p7.total_spent > 0 and pnl(p7, up, dn) >= TAKE_PROFIT:
                p7.cash_out(up, dn); continue
            if p7.cycle_profit >= CYCLE_CAP: continue
            u = p7.positions["up"]; d = p7.positions["down"]
            if up >= 65 and d == 0: p7.buy("down", 30.0, dn)
            elif dn >= 65 and u == 0: p7.buy("up",  30.0, up)
        finish("strategy_7", p7)

        # ── Strategy D/E (HighFrequencyScalper) ──────────────────────────────
        for key in ["strategy_d", "strategy_e"]:
            pS = sim(key); sc = HighFrequencyScalper()
            for tk in ticks:
                up, dn, tl = tk["up"], tk["dn"], tk["tl"]
                # Apply $1 take-profit at main loop level
                if pS.total_spent > 0 and pnl(pS, up, dn) >= TAKE_PROFIT and pS.cycle_profit < CYCLE_CAP:
                    pS.cash_out(up, dn); sc.reset_state(); continue
                if pS.cycle_profit >= CYCLE_CAP: continue
                acts = sc.on_tick(tl, up, dn, pS)
                for act in acts:
                    if act["action"] in ("market_sell", "limit_sell_fill"):
                        rev = act["shares"] * (act["price"] / 100.0)
                        pS.balance += rev
                        pS.positions[act["side"]] = max(0, pS.positions[act["side"]] - act["shares"])
            finish(key, pS)

        # ── Strategy F (TrendScaler) ──────────────────────────────────────────
        pF = sim("strategy_f"); ts = TrendScaler()
        for tk in ticks:
            up, dn, tl = tk["up"], tk["dn"], tk["tl"]
            if pF.total_spent > 0 and pnl(pF, up, dn) >= TAKE_PROFIT and pF.cycle_profit < CYCLE_CAP:
                pF.cash_out(up, dn); ts.reset_state(); continue
            if pF.cycle_profit >= CYCLE_CAP: continue
            acts = ts.on_tick(tl, up, dn, pF)
            for act in acts:
                if act["action"] in ("market_sell", "limit_sell_fill"):
                    rev = act["shares"] * (act["price"] / 100.0)
                    pF.balance += rev
                    pF.positions[act["side"]] = max(0, pF.positions[act["side"]] - act["shares"])
        finish("strategy_f", pF)

        # ── C+Trailing (directional scalp with $1 TP) ────────────────────────
        pCT = sim("strategy_c_trailing"); ct_held = None
        for tk in ticks:
            up, dn, tl = tk["up"], tk["dn"], tk["tl"]
            if tl <= 60: break
            if ct_held:
                p2 = pnl(pCT, up, dn)
                if p2 >= TAKE_PROFIT:
                    pCT.cash_out(up, dn); ct_held = None; continue
                if p2 <= -2.0 and tl <= 2400: pCT.cash_out(up, dn); ct_held = None; continue
            if ct_held is None and pCT.cycle_profit < CYCLE_CAP:
                m = mt(tk["t"])
                if m == "up" and up <= 45:
                    if pCT.buy("up", 30.0, up): ct_held = "up"
                elif m == "down" and dn <= 45:
                    if pCT.buy("down", 30.0, dn): ct_held = "down"
        finish("strategy_c_trailing", pCT)

        # ── CPT (directional scalp & repeat with $1 TP) ──────────────────────
        pCPT = sim("strategy_cpt"); cpt_held = None
        for tk in ticks:
            up, dn, tl = tk["up"], tk["dn"], tk["tl"]
            if tl <= 60: break
            if cpt_held:
                p2 = pnl(pCPT, up, dn)
                if p2 >= TAKE_PROFIT:
                    pCPT.cash_out(up, dn); cpt_held = None; continue
                if p2 <= -2.0 and tl <= 2400: pCPT.cash_out(up, dn); cpt_held = None; continue
            if cpt_held is None and pCPT.cycle_profit < CYCLE_CAP:
                if not (tl <= 900 and (up <= 20 or dn <= 20)):
                    m = mt(tk["t"])
                    if m == "up" and up <= 50:
                        if pCPT.buy("up", 30.0, up): cpt_held = "up"
                    elif m == "down" and dn <= 50:
                        if pCPT.buy("down", 30.0, dn): cpt_held = "down"
        finish("strategy_cpt", pCPT)

        # ── Claude AI (price-based, no real-time dependency) ─────────────────
        # Observation: skip first 36 ticks (3 min). Enter on momentum with EMA.
        # Exit: $1 TP, $1.50+ trailing ($0.15 drop), last-10min $0.50 TP.
        pCL = sim("strategy_claude")
        cl_peak = 0.0; cl_held = None; ema_fast = None; ema_slow = None
        for i, tk in enumerate(ticks):
            up, dn, tl = tk["up"], tk["dn"], tk["tl"]
            if i < 36: continue  # 3-min observation window
            # Update EMA on current price
            p_cur = tk["price"]
            ema_fast = p_cur if ema_fast is None else ema_fast * 0.9 + p_cur * 0.1
            ema_slow = p_cur if ema_slow is None else ema_slow * 0.97 + p_cur * 0.03
            if cl_held:
                p2 = pnl(pCL, up, dn)
                if p2 > cl_peak: cl_peak = p2
                dyn = 0.50 if tl <= 300 else (0.70 if tl <= 600 else TAKE_PROFIT)
                if dyn <= p2 <= 1.50:
                    pCL.cash_out(up, dn); cl_held = None; cl_peak = 0.0; continue
                if cl_peak > 1.50 and p2 <= cl_peak - 0.15:
                    pCL.cash_out(up, dn); cl_held = None; cl_peak = 0.0; continue
                if tl <= 600 and p2 <= -3.0:
                    pCL.cash_out(up, dn); cl_held = None; cl_peak = 0.0; continue
            if cl_held is None and pCL.cycle_profit < CYCLE_CAP and tl > 60:
                max_dollars = 5.0 if tl < 900 else 10.0
                target_shares = min(30.0, int(max_dollars / (min(up, dn) / 100.0 + 0.001)))
                if ema_fast and ema_slow:
                    if ema_fast > ema_slow * 1.0003 and up <= 55:
                        if pCL.buy("up", target_shares, up): cl_held = "up"
                    elif ema_fast < ema_slow * 0.9997 and dn <= 55:
                        if pCL.buy("down", target_shares, dn): cl_held = "down"
        finish("strategy_claude", pCL)

    # Build results
    results = []
    for key, label in labels.items():
        final_bal = round(START_BAL + totals[key], 2)
        net       = round(totals[key], 2)
        tc        = t_count[key]
        win_pct   = round((t_wins[key] / tc) * 100) if tc > 0 else 0
        results.append({
            "key": key, "label": label,
            "final_balance": final_bal,
            "net_profit":    net,
            "roi_pct":       round((net / START_BAL) * 100, 1),
            "trades":        tc,
            "win_pct":       win_pct,
        })
    results.sort(key=lambda r: r["final_balance"], reverse=True)
    return {"cycles": cycles, "start_balance": START_BAL, "results": results}


class MarketState:
    def __init__(self):
        self.current_price = 0.0
        self.price_to_beat = 0.0
        self.cycle_start_time = 0.0
        self.cycle_end_time = 0.0
        self.cycle_duration = 3600.0 # Default 1 hour
        self.up_price = 50
        self.down_price = 50
        self.volatility = 0.010
        self.peak_profit = 0.0
        self.history = [] # For strategy B momentum sniper (price, timestamp)
        self.is_live = False
        self.live_slug = ""
        self.prices_loaded = False  # True only after first real CLOB price update
        self.transitioning = False  # True while searching for next market (prevents simulator 1c/99c)
        
    def reset_cycle(self, price: float):
        self.price_to_beat = price
        now = time.time()
        self.cycle_start_time = now
        self.cycle_end_time = now + self.cycle_duration
        self.peak_profit = 0.0
        
    def resolve_cycle(self):
        up_wins = self.current_price > self.price_to_beat
        down_wins = self.current_price < self.price_to_beat
        # if equal, maybe down wins or house wins, let's say down wins for simplicity
        if not down_wins and not up_wins:
            down_wins = True
        return up_wins, down_wins

    def get_time_left_seconds(self):
        return max(0, self.cycle_end_time - time.time())
        
    def update_pricing(self):
        if self.is_live:
            return # Override: Gamma API updates prices continuously

        if self.transitioning:
            return  # Searching for next live market — don't override prices with simulator settlement

        if self.price_to_beat == 0.0:
            return

        time_left = self.get_time_left_seconds()

        # Edge case: Market expired
        if time_left == 0:
            if self.current_price > self.price_to_beat:
                self.up_price = 99
                self.down_price = 1
            else:
                self.up_price = 1
                self.down_price = 99
            return
            
        # Convert time to a fraction of the full cycle
        time_fraction = max(0.001, time_left / self.cycle_duration)
        
        # Calculate the Z-score (distance to target weighted by time and volatility)
        price_difference_pct = (self.current_price - self.price_to_beat) / self.price_to_beat
        z_score = price_difference_pct / (self.volatility * math.sqrt(time_fraction))
        
        # Get the probability using the Normal CDF (0.0 to 1.0)
        raw_up_prob = 0.5 * (1 + math.erf(z_score / math.sqrt(2)))
        
        # Convert to cents and clamp between 1¢ and 99¢
        up_cents = max(1, min(99, round(raw_up_prob * 100)))
        down_cents = 100 - up_cents
        
        # Apply the 1% Simulator Spread/Fee (Market Maker edge)
        self.up_price = min(99, up_cents + 1)
        self.down_price = min(99, down_cents + 1)

class Portfolio:
    def __init__(self):
        self.balance = 10000.0
        self.positions = {'up': 0.0, 'down': 0.0}
        self.spent = {'up': 0.0, 'down': 0.0}
        self.total_spent = 0.0
        self.cycle_profit = 0.0 # Track profit recognized in the current cycle
        self.history = []
        
    def reset_for_cycle(self, up_wins: bool, down_wins: bool):
        payout = 0.0
        if up_wins:
            payout += self.positions['up'] * 1.00
            winning_side = 'UP'
        elif down_wins:
            payout += self.positions['down'] * 1.00
            winning_side = 'DOWN'
        else:
            winning_side = 'TIE'
            
        self.balance += payout
        if self.positions['up'] > 0 or self.positions['down'] > 0:
            profit_for_trade = payout - self.total_spent
            self.cycle_profit += profit_for_trade
            self.history.append({
                'time': datetime.now().strftime("%H:%M:%S"),
                'winning_side': winning_side,
                'up_shares': round(self.positions['up'], 2),
                'down_shares': round(self.positions['down'], 2),
                'spent': round(self.total_spent, 2),
                'payout': round(payout, 2),
                'profit': round(profit_for_trade, 2)
            })
            # keep only last 50
            if len(self.history) > 50:
                self.history = self.history[-50:]

        self.positions = {'up': 0.0, 'down': 0.0}
        self.spent = {'up': 0.0, 'down': 0.0}
        self.total_spent = 0.0
        self.cycle_profit = 0.0 # Reset for the new cycle

    def cash_out(self, up_price: int, down_price: int):
        up_value = self.positions['up'] * (up_price / 100.0)
        down_value = self.positions['down'] * (down_price / 100.0)
        total_value = up_value + down_value
        profit = total_value - self.total_spent
        self.cycle_profit += profit # Accumulate the early cash-out profit toward the cycle target
        
        self.balance += total_value
        if self.positions['up'] > 0 or self.positions['down'] > 0:
            self.history.append({
                'time': datetime.now().strftime("%H:%M:%S"),
                'winning_side': 'EARLY_EXIT',
                'up_shares': round(self.positions['up'], 2),
                'down_shares': round(self.positions['down'], 2),
                'spent': round(self.total_spent, 2),
                'payout': round(total_value, 2),
                'profit': round(profit, 2)
            })
            if len(self.history) > 50:
                self.history = self.history[-50:]

        self.positions = {'up': 0.0, 'down': 0.0}
        self.spent = {'up': 0.0, 'down': 0.0}
        self.total_spent = 0.0

    def buy(self, direction: str, shares: float, price_cents: int):
        cost = shares * (price_cents / 100.0)
        if self.balance >= cost:
            self.balance -= cost
            self.total_spent += cost
            self.spent[direction] += cost
            self.positions[direction] += shares
            return True
        return False

market = MarketState()
paper_portfolio = Portfolio()
live_portfolio = Portfolio()
live_portfolio.balance = float(os.getenv("INITIAL_BALANCE", "0.0"))
portfolio = paper_portfolio

# Strategies State
active_strategy_a = False
active_strategy_b = False
active_strategy_c = False
active_strategy_c_trailing = False
active_strategy_d = False
active_strategy_e = False
active_strategy_f = False
active_strategy_7 = False
active_strategy_cpt = False  # Strategy C+ Trail
active_strategy_claude = False  # Claude AI Confluence Strategy
cpt_entry_time = 0.0  # Timestamp when C+ Trail position was opened
global_profit_target = 4.0

# Per-strategy performance stats (persist across cycles, reset manually)
_stat_keys = ["strategy_a","strategy_b","strategy_c","strategy_c_trailing",
               "strategy_d","strategy_e","strategy_f","strategy_7","strategy_cpt","strategy_claude"]
strategy_stats = {k: {"trades":0,"wins":0,"total_profit":0.0,"best":0.0,"worst":0.0} for k in _stat_keys}

def record_stat(strategy_key: str, profit: float):
    if strategy_key in strategy_stats:
        s = strategy_stats[strategy_key]
        s["trades"] += 1
        if profit > 0:
            s["wins"] += 1
        s["total_profit"] = round(s["total_profit"] + profit, 2)
        s["best"]  = round(max(s["best"],  profit), 2)
        s["worst"] = round(min(s["worst"], profit), 2)
strategy_a_strikes = 0
strategy_a_done = False
strategy_b_trade_done = False
live_mode_enabled = os.getenv("LIVE_TRADING_ENABLED", "FALSE").upper() == "TRUE"  # Auto-set from .env

# ── Settings persistence ────────────────────────────────────────────────────
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")

def save_settings():
    """Persist active strategy and trading settings to disk so they survive server restarts."""
    data = {
        "active_strategy_a": active_strategy_a,
        "active_strategy_b": active_strategy_b,
        "active_strategy_c": active_strategy_c,
        "active_strategy_c_trailing": active_strategy_c_trailing,
        "active_strategy_d": active_strategy_d,
        "active_strategy_e": active_strategy_e,
        "active_strategy_f": active_strategy_f,
        "active_strategy_7": active_strategy_7,
        "active_strategy_cpt": active_strategy_cpt,
        "active_strategy_claude": active_strategy_claude,
        "global_profit_target": global_profit_target if global_profit_target != float('inf') else -1,
    }
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[SETTINGS] Save error: {e}")

def load_settings():
    """Load persisted settings on startup."""
    global active_strategy_a, active_strategy_b, active_strategy_c, active_strategy_c_trailing
    global active_strategy_d, active_strategy_e, active_strategy_f, active_strategy_7
    global active_strategy_cpt, active_strategy_claude, global_profit_target
    try:
        if not os.path.exists(SETTINGS_FILE):
            return
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        active_strategy_a         = data.get("active_strategy_a", False)
        active_strategy_b         = data.get("active_strategy_b", False)
        active_strategy_c         = data.get("active_strategy_c", False)
        active_strategy_c_trailing= data.get("active_strategy_c_trailing", False)
        active_strategy_d         = data.get("active_strategy_d", False)
        active_strategy_e         = data.get("active_strategy_e", False)
        active_strategy_f         = data.get("active_strategy_f", False)
        active_strategy_7         = data.get("active_strategy_7", False)
        active_strategy_cpt       = data.get("active_strategy_cpt", False)
        active_strategy_claude    = data.get("active_strategy_claude", False)
        pt = data.get("global_profit_target", 4.0)
        global_profit_target      = float('inf') if pt == -1 else float(pt)
        print(f"[SETTINGS] Loaded — active: {[k for k,v in data.items() if v is True]}")
    except Exception as e:
        print(f"[SETTINGS] Load error: {e}")

load_settings()  # Apply persisted settings immediately at import time
WARMUP_SECONDS = 0  # Warmup removed — per-strategy guards handle this (prices_loaded, Claude 3min observation, stale filter)
cycle_warmup_until = 0.0  # No warmup

# Token IDs for the current live market (loaded from tokens.json or via CLOB lookup)
live_token_ids = {"up": "", "down": ""}
try:
    with open("tokens.json", "r") as f:
        tokens = json.load(f)
        if len(tokens) >= 2:
            live_token_ids["up"] = tokens[0]
            live_token_ids["down"] = tokens[1]
except:
    pass

async def cancel_all_open_orders():
    """Cancel all open orders on the CLOB at cycle boundaries."""
    client = clob_clients.get(0) or clob_clients.get(1) or clob_clients.get(2)
    if not client or not live_mode_enabled:
        return
    try:
        await asyncio.to_thread(client.cancel_all)
        print("[LIVE] Cancelled all open orders for new cycle")
    except Exception as e:
        print(f"[LIVE] Cancel all failed: {e}")

async def _try_order(order_args: "OrderArgs", order_type: "OrderType") -> tuple:
    """Place order. BTC Up/Down = neg_risk=True (NegRiskCTFExchange). EOA sig_type=0 first."""
    for sig_type in [0, 1, 2]:  # EOA first — raw private key = sig_type=0
        client = clob_clients.get(sig_type)
        if not client:
            continue
        for neg_risk in [True, False]:  # neg_risk=True for BTC Up/Down markets
            try:
                try:
                    from py_clob_client.clob_types import CreateOrderOptions
                    # tick_size=0.01 for BTC hourly markets, neg_risk via options
                    opts = CreateOrderOptions(neg_risk=neg_risk, tick_size="0.01")
                    signed = await asyncio.to_thread(client.create_order, order_args, opts)
                except (ImportError, TypeError):
                    signed = await asyncio.to_thread(client.create_order, order_args)
                resp = await asyncio.to_thread(client.post_order, signed, order_type)
                print(f"[LIVE] ✅ Order placed (sig_type={sig_type}, neg_risk={neg_risk}) → {resp}")
                return True, resp
            except Exception as e:
                err = str(e)
                print(f"[LIVE] sig_type={sig_type} neg_risk={neg_risk}: {err[:120]}")
                if any(x in err for x in ["invalid signature", "Unauthorized", "not supported", "invalid amounts"]):
                    continue
                if "not enough balance" in err or "allowance" in err.lower():
                    for ws in clients:
                        try:
                            await ws.send_text('{"type":"error","message":"Insufficient CLOB balance — deposit USDC to your Polymarket wallet to enable live trading."}')
                        except Exception:
                            pass
                return False, err
    return False, "all combinations failed"

async def execute_live_buy(direction: str, shares: float, price_cents: int):
    """Execute a real market buy on Polymarket CLOB."""
    if not clob_clients or not live_mode_enabled:
        return
    token_id = live_token_ids.get(direction, "")
    if not token_id:
        print(f"[LIVE] ⚠️ No token ID for {direction} — order SKIPPED (simulation only)")
        return
    # Aggressive price (+2¢) to cross the spread as taker
    price = round(min(0.97, (price_cents + 2) / 100.0), 2)
    size = round(shares, 2)
    order_args = OrderArgs(price=price, size=size, side=BUY, token_id=token_id)
    print(f"[LIVE] Placing BUY {size} {direction.upper()} @ {int(price*100)}¢")
    await _try_order(order_args, OrderType.FOK)

async def execute_live_sell(direction: str, shares: float, price_cents: int):
    """Execute a real market sell on Polymarket CLOB."""
    if not clob_clients or not live_mode_enabled:
        return
    token_id = live_token_ids.get(direction, "")
    if not token_id:
        print(f"[LIVE] ⚠️ No token ID for {direction} — sell SKIPPED (simulation only)")
        return
    # Aggressive price (-2¢) to cross the spread as taker
    price = round(max(0.02, (price_cents - 2) / 100.0), 2)
    size = round(shares, 2)
    order_args = OrderArgs(price=price, size=size, side=SELL, token_id=token_id)
    print(f"[LIVE] Placing SELL {size} {direction.upper()} @ {int(price*100)}¢")
    await _try_order(order_args, OrderType.FOK)

clients: List[WebSocket] = []

async def broadcast_state():
    # calculate live P&L
    up_value = portfolio.positions['up'] * (market.up_price / 100.0)
    down_value = portfolio.positions['down'] * (market.down_price / 100.0)
    live_value = up_value + down_value
    live_profit = live_value - portfolio.total_spent
    
    state_msg = {
        "type": "state_update",
        "data": {
            "market": {
                "current_price": market.current_price,
                "price_to_beat": market.price_to_beat,
                "time_left": market.get_time_left_seconds(),
                "cycle_duration": market.cycle_duration,
                "volatility": market.volatility,
                "up_price": market.up_price,
                "down_price": market.down_price,
                "is_live": market.is_live,
                "live_slug": market.live_slug,
                "warmup_seconds_left": max(0, round(cycle_warmup_until - time.time())),
            },
            "portfolio": {
                "balance": portfolio.balance,
                "positions": portfolio.positions,
                "spent": portfolio.spent,
                "total_spent": portfolio.total_spent,
                "history": portfolio.history,
                "live_profit": live_profit,
                "live_profit_up": up_value - portfolio.spent['up'],
                "live_profit_down": down_value - portfolio.spent['down'],
                "live_value_up": up_value,
                "live_value_down": down_value,
                "live_value": live_value
            },
            "strategies": {
                "strategy_a": active_strategy_a,
                "strategy_b": active_strategy_b,
                "strategy_c": active_strategy_c,
                "strategy_c_trailing": active_strategy_c_trailing,
                "strategy_d": active_strategy_d,
                "strategy_e": active_strategy_e,
                "strategy_f": active_strategy_f,
                "strategy_7": active_strategy_7,
                "strategy_cpt": active_strategy_cpt,
                "strategy_claude": active_strategy_claude,
                "claude_confidence": claude_strategy.confidence,
                "claude_phase": claude_strategy.phase,
                "claude_exit_reason": claude_strategy.exit_reason,
                "profit_target": global_profit_target if global_profit_target != float('inf') else -1,
                "strikes": strategy_a_strikes,
                "b_done": strategy_b_trade_done,
                "live_mode": live_mode_enabled,
                "live_available": LIVE_TRADING_AVAILABLE
            },
            "strategy_stats": strategy_stats
        }
    }
    msg_str = json.dumps(state_msg)
    disconnected = []
    for client in clients:
        try:
            await client.send_text(msg_str)
        except:
            disconnected.append(client)
    for c in disconnected:
        clients.remove(c)

async def sync_live_balance():
    """Periodically try to sync USDC balance from Polymarket CLOB."""
    while True:
        await asyncio.sleep(60)
        try:
            if not clob_client or not live_mode_enabled:
                continue
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            for sig_type in [0, 1, 2]:
                try:
                    def _get_bal(st=sig_type):
                        return clob_client.get_balance_allowance(
                            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=st)
                        )
                    data = await asyncio.to_thread(_get_bal)
                    bal_raw = data.get("balance", "0") if isinstance(data, dict) else "0"
                    usdc = float(bal_raw) / 1e6  # CLOB returns microUSDC (6 decimals)
                    if usdc > 0:
                        live_portfolio.balance = usdc
                        print(f"[LIVE] Balance synced from CLOB: ${usdc:.2f}")
                        break
                except Exception:
                    pass
        except Exception as e:
            print(f"[LIVE] Balance sync error: {e}")

async def market_loop():
    global active_strategy_a, active_strategy_b, active_strategy_claude
    global strategy_a_strikes, strategy_b_trade_done, strategy_a_done
    global cycle_warmup_until

    while True:
        global portfolio
        portfolio = live_portfolio if live_mode_enabled else paper_portfolio
        try:
            now = time.time()
                
            # Initialize or resolve cycle
            if market.cycle_end_time == 0.0 or now >= market.cycle_end_time:
                if market.cycle_end_time != 0.0:
                    up_wins, down_wins = market.resolve_cycle()
                    portfolio.reset_for_cycle(up_wins, down_wins)
                
                # Start new cycle
                market.reset_cycle(market.current_price)
                strategy_a_strikes = 0
                strategy_b_trade_done = False
                strategy_a_done = False
                scalper_4.reset_state()
                scalper_5.reset_state()
                scalper_6.reset_state()
                claude_strategy.reset_state(full_reset=True)
                market.prices_loaded = False  # Force wait for fresh prices on new cycle
                asyncio.create_task(cancel_all_open_orders())  # Cancel stale GTC/resting orders
                print(f"[CYCLE] New cycle started. Per-strategy guards active (prices_loaded, Claude observation).")
                
            # Update dynamic pricing
            market.update_pricing()
            
            time_left = market.get_time_left_seconds()
            
            # Check for Early Profit Exit
            sunk = portfolio.total_spent
            if sunk > 0:
                up_val = portfolio.positions['up'] * (market.up_price / 100.0)
                down_val = portfolio.positions['down'] * (market.down_price / 100.0)
                live_profit = (up_val + down_val) - sunk
                
                if active_strategy_c_trailing or active_strategy_cpt or active_strategy_claude:
                    # Time-Decaying Profit Target
                    dynamic_target = 1.0
                    if time_left <= 300:   # last 5 min: take $0.50+
                        dynamic_target = 0.50
                    elif time_left <= 600: # last 10 min: take $0.70+
                        dynamic_target = 0.70
                    # 10-15 min: keep $1.00 (normal target)

                    market.peak_profit = max(live_profit, market.peak_profit)

                    did_exit = False

                    # Claude + C+Trailing + CPT: same exit logic
                    # 1. Instant sell when profit hits $1.00–$1.50
                    if dynamic_target <= live_profit <= 1.50:
                        did_exit = True
                    # 2. Trailing sell: spiked > $1.50, exit when drops $0.15 from peak
                    elif market.peak_profit > 1.50 and live_profit <= (market.peak_profit - 0.15):
                        did_exit = True
                    # (No panic sell — let stop-loss handle the downside)

                    if did_exit:
                        _active_key = "strategy_claude" if active_strategy_claude else ("strategy_cpt" if active_strategy_cpt else "strategy_c_trailing")
                        record_stat(_active_key, live_profit)
                        portfolio.cash_out(market.up_price, market.down_price)
                        market.peak_profit = 0.0
                        strategy_a_strikes = 0
                        scalper_4.reset_state()
                        scalper_5.reset_state()
                        scalper_6.reset_state()
                        claude_strategy.reset_state()
                        if active_strategy_a:
                            strategy_a_done = True
                        continue
                else:
                    # Original logic: Book profit at exactly $1.00 immediately for all other strategies
                    if live_profit >= 1.0:
                        _active_key = "strategy_a" if active_strategy_a else ("strategy_b" if active_strategy_b else ("strategy_c" if active_strategy_c else "strategy_d"))
                        record_stat(_active_key, live_profit)
                        portfolio.cash_out(market.up_price, market.down_price)
                        market.peak_profit = 0.0
                        strategy_a_strikes = 0
                        scalper_4.reset_state()
                        scalper_5.reset_state()
                        scalper_6.reset_state()
                        claude_strategy.reset_state()
                        if active_strategy_a:
                            strategy_a_done = True
                        continue # Skip remainder of loop

            # Block all trading during warmup, until live prices loaded, or until token IDs ready in live mode
            tokens_ready = not live_mode_enabled or (bool(live_token_ids.get("up")) and bool(live_token_ids.get("down")))
            if time.time() < cycle_warmup_until or (market.is_live and not market.prices_loaded) or not tokens_ready:
                await asyncio.sleep(1)
                continue

            # Strategy A logic (Smart Balancer)
            if active_strategy_a and time_left > 10:
                target_shares = 30.0
                up_pos = portfolio.positions['up']
                down_pos = portfolio.positions['down']
                up_spent = portfolio.spent['up']
                down_spent = portfolio.spent['down']
                
                avg_up = (up_spent / up_pos * 100) if up_pos > 0 else 0
                avg_down = (down_spent / down_pos * 100) if down_pos > 0 else 0
                
                # Check Cash Out condition (if we hold both enough to guarantee profit and want to close early, 
                # but for this strategy we usually let it ride to exactly 100c payout).
                
                if up_pos < target_shares:
                    shares_to_buy = target_shares - up_pos
                    if down_pos > 0:
                        # We have down, need to buy up cheaply enough to guarantee profit (<96c combined)
                        max_price = 96 - avg_down
                        if market.up_price <= max_price:
                            portfolio.buy('up', shares_to_buy, market.up_price)
                    else:
                        # We have no positions. Enter either a heavy favorite or a deep underdog
                        if market.up_price <= 15 or (85 <= market.up_price <= 90):
                            portfolio.buy('up', shares_to_buy, market.up_price)

                if down_pos < target_shares:
                    shares_to_buy = target_shares - down_pos
                    if up_pos > 0:
                        max_price = 96 - avg_up
                        if market.down_price <= max_price:
                            portfolio.buy('down', shares_to_buy, market.down_price)
                    else:
                        # We have no up_pos. Check if we should enter down.
                        if market.down_price <= 15 or (85 <= market.down_price <= 90):
                            portfolio.buy('down', shares_to_buy, market.down_price)

            # Strategy B logic (Momentum Sniper with Smart Take Profit)
            if active_strategy_b and time_left > 30: # > 30s left
                 if not strategy_b_trade_done:
                     # Monitor 1m trend
                     one_min_ago = now - 60
                     hist_1m = [h for h in market.history if h[0] >= one_min_ago]
                     if len(hist_1m) > 10:
                         first_price = hist_1m[0][1]
                         last_price = hist_1m[-1][1]
                         pct_change = (last_price - first_price) / first_price
                         
                         if pct_change >= 0.0002: # Spike 
                             if market.up_price < 80: # Don't buy the absolute top
                                 success = portfolio.buy('up', 15.0, market.up_price)
                                 if success:
                                     strategy_b_trade_done = True
                         elif pct_change <= -0.0002: # Drop
                             if market.down_price < 80:
                                 success = portfolio.buy('down', 15.0, market.down_price)
                                 if success:
                                     strategy_b_trade_done = True
                 else:
                     # We are in a trade! Actively manage profit like a day trader.
                     up_spent = portfolio.spent['up']
                     down_spent = portfolio.spent['down']
                     
                     if up_spent > 0 and down_spent == 0:
                         # We hold UP
                         up_val = portfolio.positions['up'] * (market.up_price / 100.0)
                         profit = up_val - up_spent
                         # Take profit if +$1.00 OR +30% ROI
                         if profit >= 1.0 or (up_val / up_spent) >= 1.30:
                             portfolio.cash_out(market.up_price, market.down_price)
                             strategy_b_trade_done = False # Ready to hunt again
                         # Stop loss at -40%
                         elif (up_val / up_spent) <= 0.60:
                             portfolio.cash_out(market.up_price, market.down_price)
                             strategy_b_trade_done = False
                             
                     elif down_spent > 0 and up_spent == 0:
                         # We hold DOWN
                         down_val = portfolio.positions['down'] * (market.down_price / 100.0)
                         profit = down_val - down_spent
                         # Take profit if +$1.00 OR +30% ROI
                         if profit >= 1.0 or (down_val / down_spent) >= 1.30:
                             portfolio.cash_out(market.up_price, market.down_price)
                             strategy_b_trade_done = False # Ready to hunt again
                         # Stop loss at -40%
                         elif (down_val / down_spent) <= 0.60:
                             portfolio.cash_out(market.up_price, market.down_price)
                             strategy_b_trade_done = False
                            
            # Strategy C logic (Market Maker Arbitrage - Intelligent)
            # Places limit orders at <= 45¢ on BOTH sides, but only enters if market is chopping (not strongly trending).
            # Enforces cycle profit targets and halts trading with < 60s left.
            if active_strategy_c and time_left > 60:
                target_profit = global_profit_target
                if portfolio.cycle_profit < target_profit:
                    target_shares = 30.0
                    up_pos = portfolio.positions['up']
                    down_pos = portfolio.positions['down']
                    
                    # Calculate 1m trend to detect if it's a safe choppy market
                    one_min_ago = now - 60
                    hist_1m = [h for h in market.history if h[0] >= one_min_ago]
                    is_choppy = True
                    if len(hist_1m) > 10:
                        first_price = hist_1m[0][1]
                        last_price = hist_1m[-1][1]
                        pct_change = abs((last_price - first_price) / first_price)
                        # If the price changed by more than 0.15% in the last minute, it's trending, not chopping.
                        if pct_change >= 0.0015:
                            is_choppy = False
                    
                    # Only require 'is_choppy' for the FIRST leg.
                    # If we already have one leg, we MUST try to buy the other leg to complete the arb.
                    can_enter_up = is_choppy or (down_pos >= target_shares)
                    can_enter_down = is_choppy or (up_pos >= target_shares)
                    
                    # Try to buy UP if <= 45c
                    if up_pos < target_shares and can_enter_up:
                        shares_to_buy = target_shares - up_pos
                        if market.up_price <= 45:
                            portfolio.buy('up', shares_to_buy, market.up_price)
                            
                    # Try to buy DOWN if <= 45c
                    if down_pos < target_shares and can_enter_down:
                        shares_to_buy = target_shares - down_pos
                        if market.down_price <= 45:
                            portfolio.buy('down', shares_to_buy, market.down_price)

            # Strategy C Trailing logic (Market Maker Arbitrage - Intelligent with Trailing Stop)
            if active_strategy_c_trailing and time_left > 60:
                target_profit = global_profit_target
                if portfolio.cycle_profit < target_profit:
                    target_shares = 30.0
                    up_pos = portfolio.positions['up']
                    down_pos = portfolio.positions['down']
                    
                    up_spent = portfolio.spent['up']
                    down_spent = portfolio.spent['down']
                    
                    # 1. Advanced AI Trend Analysis (Micro vs Macro)
                    four_min_ago = now - 240
                    fifteen_min_ago = now - 900
                    one_min_ago = now - 60
                    
                    hist_1m = [h for h in market.history if h[0] >= one_min_ago]
                    hist_4m = [h for h in market.history if h[0] >= four_min_ago]
                    hist_15m = [h for h in market.history if h[0] >= fifteen_min_ago]
                    
                    micro_trend = "neutral"
                    macro_trend = "neutral"
                    trend = "neutral"
                    micro_bounce = "neutral" # Aggressive short-term 1m bounce direction

                    if len(hist_15m) > 10:
                        last_p = hist_15m[-1][1]
                        # 15m Macro Trend
                        first_15m = hist_15m[0][1]
                        if last_p > first_15m * 1.0015: macro_trend = "up"
                        elif last_p < first_15m * 0.9985: macro_trend = "down"
                        
                        # 4m Standard Trend
                        first_4m = hist_4m[0][1] if len(hist_4m) > 0 else last_p
                        if last_p > first_4m * 1.0005: trend = "up"
                        elif last_p < first_4m * 0.9995: trend = "down"

                        # 1m Micro Trend & Bounce detection (Sniper)
                        if len(hist_1m) > 3:
                            first_1m = hist_1m[0][1]
                            if last_p > first_1m * 1.0002:
                                micro_trend = "up"
                                micro_bounce = "up"
                            elif last_p < first_1m * 0.9998:
                                micro_trend = "down"
                                micro_bounce = "down"
                                
                    # 2. Market Exhaustion Guard (RSI Simulation)
                    # Measure continuous movement without pullback in the last 4m. If it moved >0.2% straight, it's exhausted.
                    is_exhausted_up = False
                    is_exhausted_down = False
                    if len(hist_4m) > 5:
                        first_4m = hist_4m[0][1]
                        last_p = hist_4m[-1][1]
                        if last_p > first_4m * 1.002: is_exhausted_up = True
                        if last_p < first_4m * 0.998: is_exhausted_down = True

                    
                    # Intelligent 15-minute Skew Guard
                    # If <=15 mins left, profit < $4.00, and odds heavily skewed (<=20c / >=80c), halt new entries.
                    is_last_15m = time_left <= 900
                    profit_under_4 = portfolio.cycle_profit < 4.0
                    is_skewed = (market.up_price <= 20 or market.down_price <= 20)
                    halt_trading = is_last_15m and profit_under_4 and is_skewed

                    # Strict Directional tracking: Only buy UP if we don't hold any DOWN, and vice versa.
                    can_enter_up = (down_pos == 0) and not halt_trading
                    can_enter_down = (up_pos == 0) and not halt_trading
                    
                    if can_enter_up:
                        # Advanced Entry: Only buy UP if Micro and Macro align OR Micro is strongly UP (not exhausted)
                        is_aligned_up = (micro_trend == "up" and macro_trend != "down") or (micro_trend == "up" and trend == "up")
                        
                        if market.up_price <= 45 and up_pos < target_shares and is_aligned_up and not is_exhausted_up:
                            shares_to_buy = target_shares - up_pos
                            portfolio.buy('up', shares_to_buy, market.up_price)
                            
                        # Sniper Underdog Strategy: Wait for 1m bounce instead of blind falling knife
                        elif time_left >= 1800 and market.up_price <= 15 and up_spent < 5.50:
                            if micro_bounce == "up": # Waiting for the exact bounce
                                budget_left = 5.50 - up_spent
                                shares_to_buy = int(budget_left / (market.up_price / 100.0))
                                if shares_to_buy > 0:
                                    portfolio.buy('up', shares_to_buy, market.up_price)
                            
                    if can_enter_down:
                        # Advanced Entry: Only buy DOWN if Micro and Macro align OR Micro is strongly DOWN (not exhausted)
                        is_aligned_down = (micro_trend == "down" and macro_trend != "up") or (micro_trend == "down" and trend == "down")
                        
                        if market.down_price <= 45 and down_pos < target_shares and is_aligned_down and not is_exhausted_down:
                            shares_to_buy = target_shares - down_pos
                            portfolio.buy('down', shares_to_buy, market.down_price)
                            
                        # Sniper Underdog Strategy >30mins left
                        elif time_left >= 1800 and market.down_price <= 15 and down_spent < 5.50:
                            if micro_bounce == "down": # Waiting for the exact bounce
                                budget_left = 5.50 - down_spent
                                shares_to_buy = int(budget_left / (market.down_price / 100.0))
                                if shares_to_buy > 0:
                                    portfolio.buy('down', shares_to_buy, market.down_price)

            # Strategy C+ Trail logic (Directional Scalp & Repeat)
            # Buys ONE side based on micro trend, books profit quickly, re-enters, repeats until $4 target
            if active_strategy_cpt and time_left > 60:
                target_profit = global_profit_target
                if portfolio.cycle_profit < target_profit:
                    target_shares = 30.0
                    up_pos = portfolio.positions['up']
                    down_pos = portfolio.positions['down']
                    sunk_cpt = portfolio.total_spent

                    # --- Time-Based Stop-Loss ---
                    # First 20 mins (60→40 min left): NO stop-loss — give BTC time to recover
                    # Last 40 mins (40→0 min left): $2 stop-loss — time running out, cut losses
                    if sunk_cpt > 0:
                        up_val_cpt = up_pos * (market.up_price / 100.0)
                        down_val_cpt = down_pos * (market.down_price / 100.0)
                        live_pnl_cpt = (up_val_cpt + down_val_cpt) - sunk_cpt

                        # 10-Minute Timeout: If holding >10 min and in ANY profit (>=$0.10), cash out & re-enter
                        if cpt_entry_time > 0 and (now - cpt_entry_time) >= 600 and live_pnl_cpt >= 0.10:
                            portfolio.cash_out(market.up_price, market.down_price)
                            market.peak_profit = 0.0
                            cpt_entry_time = 0.0

                        elif time_left <= 2400 and live_pnl_cpt <= -2.0:
                            # Only stop-loss if the position was entered in the last 40 min.
                            # Early entries (first 20 min) get to ride it out.
                            cpt_entry_tl = time_left + (now - cpt_entry_time) if cpt_entry_time > 0 else 0
                            if cpt_entry_tl <= 2400:
                                portfolio.cash_out(market.up_price, market.down_price)
                                market.peak_profit = 0.0
                                cpt_entry_time = 0.0
                        # Emergency stop: final 10 min with loss > $3
                        elif time_left <= 600 and live_pnl_cpt <= -3.0:
                            portfolio.cash_out(market.up_price, market.down_price)
                            market.peak_profit = 0.0
                            cpt_entry_time = 0.0

                    # --- Only enter if we have NO open position (scalp & repeat) ---
                    if up_pos == 0 and down_pos == 0:
                        # --- Trend Analysis (1-minute micro trend) ---
                        one_min_ago_cpt = now - 60
                        hist_1m_cpt = [h for h in market.history if h[0] >= one_min_ago_cpt]
                        micro_cpt = "neutral"
                        if len(hist_1m_cpt) > 3:
                            first_1m_cpt = hist_1m_cpt[0][1]
                            last_p_cpt = hist_1m_cpt[-1][1]
                            if last_p_cpt > first_1m_cpt * 1.0001:
                                micro_cpt = "up"
                            elif last_p_cpt < first_1m_cpt * 0.9999:
                                micro_cpt = "down"

                        # --- 15-Minute Skew Guard (same as Strategy C+) ---
                        # If <=15 mins left, profit < $4.00, and odds heavily skewed (<=20c), halt new entries.
                        is_last_15m_cpt = time_left <= 900
                        profit_under_4_cpt = portfolio.cycle_profit < 4.0
                        is_skewed_cpt = (market.up_price <= 20 or market.down_price <= 20)
                        halt_cpt = is_last_15m_cpt and profit_under_4_cpt and is_skewed_cpt

                        if not halt_cpt and micro_cpt != "neutral":
                            # DIRECTIONAL ENTRY: Buy the trending side ONLY
                            if micro_cpt == "up" and market.up_price <= 50:
                                portfolio.buy('up', target_shares, market.up_price)
                                market.peak_profit = 0.0
                                cpt_entry_time = now  # Track entry time

                            elif micro_cpt == "down" and market.down_price <= 50:
                                portfolio.buy('down', target_shares, market.down_price)
                                market.peak_profit = 0.0
                                cpt_entry_time = now  # Track entry time

                            # Underdog sniper: if one side is super cheap (<=25c), take the shot
                            elif micro_cpt == "up" and market.up_price <= 25:
                                portfolio.buy('up', target_shares, market.up_price)
                                market.peak_profit = 0.0
                                cpt_entry_time = now

                            elif micro_cpt == "down" and market.down_price <= 25:
                                portfolio.buy('down', target_shares, market.down_price)
                                market.peak_profit = 0.0
                                cpt_entry_time = now



            # Strategy 7 logic (Automated AMM Mean Reversion)
            if active_strategy_7 and time_left > 10:
                target_profit = global_profit_target
                
                # Debug Output occasionally to prove it's evaluating
                if int(time_left) % 10 == 0:
                    print(f"[Strategy 7 Log] AMM Prices - UP: {market.up_price}¢, DOWN: {market.down_price}¢. Target Reversion at 65¢")

                if portfolio.cycle_profit < target_profit:
                    target_shares = 30.0
                    up_pos = portfolio.positions['up']
                    down_pos = portfolio.positions['down']
                    
                    # Mean Reversion: If a side is heavily favored (>=65c), bet on the pullback
                    if market.up_price >= 65 and down_pos == 0:
                        print(f"[Strategy 7] UP is overextended ({market.up_price}¢). Entering DOWN position!")
                        shares_to_buy = target_shares - down_pos
                        portfolio.buy('down', shares_to_buy, market.down_price)
                        
                    elif market.down_price >= 65 and up_pos == 0:
                        print(f"[Strategy 7] DOWN is overextended ({market.down_price}¢). Entering UP position!")
                        shares_to_buy = target_shares - up_pos
                        portfolio.buy('up', shares_to_buy, market.up_price)

            # Strategy D, E, F logic (High-Frequency & Trend Scalers)
            for active_flag, scalper_obj in [(active_strategy_d, scalper_4), (active_strategy_e, scalper_5), (active_strategy_f, scalper_6)]:
                if active_flag:
                    actions = scalper_obj.on_tick(time_left, market.up_price, market.down_price, portfolio)
                    for act in actions:
                        if act["action"] in ["market_sell", "limit_sell_fill"]:
                            revenue = act["shares"] * (act["price"] / 100.0)
                            portfolio.balance += revenue
                            
                            if portfolio.positions[act["side"]] > 0:
                                avg_entry = portfolio.spent[act["side"]] / portfolio.positions[act["side"]]
                            else:
                                avg_entry = 0.0
                                
                            shares_to_deduct = min(act["shares"], portfolio.positions[act["side"]])
                            if shares_to_deduct < 0:
                                shares_to_deduct = 0
                                
                            portfolio.positions[act["side"]] -= shares_to_deduct
                            portfolio.spent[act["side"]] -= avg_entry * shares_to_deduct
                            
                            # Bounds prevention for floating point
                            if portfolio.spent[act["side"]] < 0 or portfolio.positions[act["side"]] <= 0:
                                portfolio.spent[act["side"]] = 0.0
                                portfolio.positions[act["side"]] = 0.0
                                
                            portfolio.total_spent = sum(portfolio.spent.values())
                            
                            # Log SCALP receipt so User can see the realized intraday PnL
                            cost_basis = avg_entry * act["shares"]
                            realized_profit = revenue - cost_basis
                            portfolio.cycle_profit += realized_profit
                            portfolio.history.append({
                                'time': datetime.now().strftime("%H:%M:%S"),
                                'winning_side': f"SCALP_{act['side'].upper()}",
                                'up_shares': round(act["shares"] if act["side"] == 'up' else 0.0, 2),
                                'down_shares': round(act["shares"] if act["side"] == 'down' else 0.0, 2),
                                'spent': round(cost_basis, 2),
                                'payout': round(revenue, 2),
                                'profit': round(realized_profit, 2)
                            })
                            if len(portfolio.history) > 10:
                                portfolio.history = portfolio.history[-10:]
                        elif act["action"] == "limit_buy_fill":
                            cost = act["shares"] * (act["price"] / 100.0)
                            if portfolio.balance >= cost:
                                portfolio.balance -= cost
                                portfolio.positions[act["side"]] += act["shares"]
                                portfolio.total_spent += cost
                                portfolio.spent[act["side"]] += cost
                            else:
                                # Revert held positions inside scalper if broke
                                scalper_obj.held_positions[act["side"]] -= act["shares"]

            # Claude Strategy: AI Confluence Engine
            if active_strategy_claude:
                # --- Global Stop-Loss & Timeout for Claude (enforced at main loop level) ---
                claude_sunk = portfolio.total_spent
                if claude_sunk > 0:
                    claude_up_val = portfolio.positions['up'] * (market.up_price / 100.0)
                    claude_down_val = portfolio.positions['down'] * (market.down_price / 100.0)
                    claude_live_pnl = (claude_up_val + claude_down_val) - claude_sunk

                    # 10-Min Timeout: only force-close small-profit positions (< $0.80).
                    # High-profit positions are managed by the trailing stop — don't cut them on a timer.
                    if claude_strategy.position_entry_time > 0 and (now - claude_strategy.position_entry_time) >= 600 and 0.10 <= claude_live_pnl < 0.80:
                        print(f"[CLAUDE] GLOBAL TIMEOUT: +${claude_live_pnl:.2f} after 10min, cashing out")
                        record_stat("strategy_claude", claude_live_pnl)
                        portfolio.cash_out(market.up_price, market.down_price)
                        market.peak_profit = 0.0
                        claude_strategy.reset_state()

                    # Hard absolute stop: -$3 on ANY position regardless of entry time.
                    # Prevents early entries from bleeding the whole cycle.
                    elif claude_live_pnl <= -3.0:
                        print(f"[CLAUDE] HARD STOP -$3 (entry_tl protection): ${claude_live_pnl:.2f}, cutting loss")
                        record_stat("strategy_claude", claude_live_pnl)
                        portfolio.cash_out(market.up_price, market.down_price)
                        market.peak_profit = 0.0
                        claude_strategy.reset_state()

                    # $2 Stop-Loss — only for positions that were ENTERED in the last 40 min.
                    # Positions entered in the first 20 min get to ride out the full cycle (still have time to recover).
                    elif time_left <= 2400 and claude_live_pnl <= -2.0:
                        entry_tl = time_left + (now - claude_strategy.position_entry_time) if claude_strategy.position_entry_time > 0 else 0
                        if entry_tl <= 2400:
                            # Entered during last 40 min → apply stop-loss
                            print(f"[CLAUDE] STOPLOSS (late entry, entry_tl={int(entry_tl)}s): ${claude_live_pnl:.2f}, cutting loss")
                            record_stat("strategy_claude", claude_live_pnl)
                            portfolio.cash_out(market.up_price, market.down_price)
                            market.peak_profit = 0.0
                            claude_strategy.reset_state()
                        else:
                            # Entered in first 20 min — let it ride, skip stop-loss
                            print(f"[CLAUDE] Holding early-entry position (entry_tl={int(entry_tl)}s, pnl=${claude_live_pnl:.2f}) — still has time to recover")
                    # Emergency stop: final 10 min, any position with loss > $3
                    elif time_left <= 600 and claude_live_pnl <= -3.0:
                        print(f"[CLAUDE] EMERGENCY STOP (last 10min): ${claude_live_pnl:.2f}, cutting loss")
                        record_stat("strategy_claude", claude_live_pnl)
                        portfolio.cash_out(market.up_price, market.down_price)
                        market.peak_profit = 0.0
                        claude_strategy.reset_state()

                actions = claude_strategy.on_tick(time_left, market.up_price, market.down_price, portfolio, market.history, portfolio.cycle_profit, global_profit_target)
                for act in actions:
                    if act["action"] in ["market_sell", "limit_sell_fill"]:
                        revenue = act["shares"] * (act["price"] / 100.0)
                        portfolio.balance += revenue

                        if portfolio.positions[act["side"]] > 0:
                            avg_entry = portfolio.spent[act["side"]] / portfolio.positions[act["side"]]
                        else:
                            avg_entry = 0.0

                        shares_to_deduct = min(act["shares"], portfolio.positions[act["side"]])
                        if shares_to_deduct < 0:
                            shares_to_deduct = 0

                        portfolio.positions[act["side"]] -= shares_to_deduct
                        portfolio.spent[act["side"]] -= avg_entry * shares_to_deduct

                        if portfolio.spent[act["side"]] < 0 or portfolio.positions[act["side"]] <= 0:
                            portfolio.spent[act["side"]] = 0.0
                            portfolio.positions[act["side"]] = 0.0

                        portfolio.total_spent = sum(portfolio.spent.values())

                        cost_basis = avg_entry * act["shares"]
                        realized_profit = revenue - cost_basis
                        portfolio.cycle_profit += realized_profit
                        claude_strategy.cycle_drawdown += min(0, realized_profit)

                        # Log exit reason
                        exit_reason = claude_strategy.exit_reason or "unknown"
                        print(f"[CLAUDE] EXIT: {exit_reason} | side={act['side']} | profit=${realized_profit:.2f} | shares={act['shares']}")

                        portfolio.history.append({
                            'time': datetime.now().strftime("%H:%M:%S"),
                            'winning_side': f"CLAUDE_{act['side'].upper()}",
                            'up_shares': round(act["shares"] if act["side"] == 'up' else 0.0, 2),
                            'down_shares': round(act["shares"] if act["side"] == 'down' else 0.0, 2),
                            'spent': round(cost_basis, 2),
                            'payout': round(revenue, 2),
                            'profit': round(realized_profit, 2)
                        })
                        if len(portfolio.history) > 10:
                            portfolio.history = portfolio.history[-10:]

                        if live_mode_enabled:
                            asyncio.create_task(execute_live_sell(act["side"], act["shares"], act["price"]))

                    elif act["action"] == "limit_buy_fill":
                        cost = act["shares"] * (act["price"] / 100.0)
                        if portfolio.balance >= cost:
                            portfolio.balance -= cost
                            portfolio.positions[act["side"]] += act["shares"]
                            portfolio.total_spent += cost
                            portfolio.spent[act["side"]] += cost

                            if live_mode_enabled:
                                asyncio.create_task(execute_live_buy(act["side"], act["shares"], act["price"]))
                        else:
                            claude_strategy.held_positions[act["side"]] -= act["shares"]

            await broadcast_state()
            await asyncio.sleep(1)
        except Exception as e:
            print("Market Loop Error:", e)
            await asyncio.sleep(1)

async def stream_live_prices():
    """Stream real-time price updates from Polymarket CLOB WebSocket (instant, no polling delay)."""
    CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    last_slug = ""

    while True:
        try:
            up_token   = live_token_ids.get("up", "")
            down_token = live_token_ids.get("down", "")
            slug       = market.live_slug

            # Wait until we have a live market with token IDs
            if not market.is_live or not up_token or not down_token:
                await asyncio.sleep(2)
                continue

            # If slug changed (new cycle), reconnect the stream
            if slug != last_slug:
                last_slug = slug

            print(f"[WS-PRICE] Connecting to CLOB stream for {slug}...")
            async with websockets.connect(CLOB_WS, ping_interval=20, ping_timeout=10) as ws:
                # Subscribe to both UP and DOWN token order-book channels
                await ws.send(json.dumps({
                    "type": "market",
                    "assets_ids": [up_token, down_token]
                }))
                print(f"[WS-PRICE] Subscribed — UP={up_token[:12]}... DOWN={down_token[:12]}...")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        # Each message is a list of events
                        events = msg if isinstance(msg, list) else [msg]
                        for ev in events:
                            asset_id = ev.get("asset_id", "")
                            # Extract best mid from bids/asks
                            bids = ev.get("bids", [])
                            asks = ev.get("asks", [])
                            if bids and asks:
                                best_bid = float(bids[0].get("price", 0))
                                best_ask = float(asks[0].get("price", 0))
                                mid = (best_bid + best_ask) / 2
                            elif ev.get("price"):
                                mid = float(ev["price"])
                            else:
                                continue

                            val = max(1, min(99, round(mid * 100)))

                            # Reject settlement/thin-book prices
                            if val <= 5 or val >= 95:
                                continue

                            # Reject any single-tick jump > 35¢ (settlement bleed-through)
                            if asset_id == up_token and abs(val - market.up_price) > 35:
                                continue
                            if asset_id == down_token and abs(val - market.down_price) > 35:
                                continue

                            if asset_id == up_token:
                                market.up_price = val
                                market.prices_loaded = True
                            elif asset_id == down_token:
                                market.down_price = val
                                market.prices_loaded = True

                            market.current_price = market.up_price
                            now = time.time()
                            market.history.append((now, market.up_price))
                            market.history = [h for h in market.history if now - h[0] <= 300]

                        # Disconnect when cycle ends so we reconnect for the new market
                        if not market.is_live or market.live_slug != last_slug:
                            break

                    except Exception:
                        continue

        except Exception as e:
            print(f"[WS-PRICE] Stream error: {e} — falling back to poll")
            # Fallback: one HTTP poll so prices don't freeze on WS error
            try:
                await _poll_live_prices()
            except Exception:
                pass
            await asyncio.sleep(3)


async def auto_discover_market():
    """Automatically discover and sync with the current Polymarket BTC Up/Down market."""
    global live_token_ids
    
    while True:
        try:
            if market.is_live and market.get_time_left_seconds() > 10:
                # Already synced — price updates come from the WS stream task
                await asyncio.sleep(5)
                continue

            # --- Cycle expired or not yet connected: reset and rediscover ---
            old_slug = market.live_slug
            if market.is_live:
                print(f"[AUTO] Cycle ended for {old_slug}, discovering new market...")
                market.is_live = False
                market.transitioning = True   # Blocks simulator from setting 1c/99c settlement prices
                market.live_slug = ""
                live_token_ids.clear()
                market.prices_loaded = False  # Block trading until new market prices confirmed

            now_secs = int(time.time())
            from datetime import timezone, timedelta
            # Auto-detect EST vs EDT using zoneinfo (correct regardless of server location)
            try:
                from zoneinfo import ZoneInfo
                et_offset = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).utcoffset()
            except Exception:
                et_offset = timedelta(hours=-4)  # fallback EDT

            # Generate slug for the CURRENT hour (the active market)
            current_time = datetime.now(timezone.utc)
            current_et = current_time + et_offset
            cur_h24 = current_et.hour
            cur_ampm = "pm" if cur_h24 >= 12 else "am"
            cur_h12 = cur_h24 % 12 or 12
            current_slug = f"bitcoin-up-or-down-{current_et.strftime('%B').lower()}-{current_et.day}-{cur_h12}{cur_ampm}-et"

            # Also try next hour (for early discovery near hour boundaries)
            next_et = current_et + timedelta(hours=1)
            nxt_h24 = next_et.hour
            nxt_ampm = "pm" if nxt_h24 >= 12 else "am"
            nxt_h12 = nxt_h24 % 12 or 12
            next_slug = f"bitcoin-up-or-down-{next_et.strftime('%B').lower()}-{next_et.day}-{nxt_h12}{nxt_ampm}-et"

            slugs_to_try = [current_slug]
            if next_slug != current_slug:
                slugs_to_try.append(next_slug)

            found_new = False
            for slug in slugs_to_try:
                if slug == old_slug:
                    continue  # Skip the expired market slug
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(
                            f"https://gamma-api.polymarket.com/events?slug={slug}",
                            timeout=10.0
                        )
                        data = resp.json()

                    if data and len(data) > 0 and not data[0].get("closed"):
                        event = data[0]
                        markets_list = event.get("markets", [])
                        for m in markets_list:
                            if not m.get("closed") and m.get("active"):
                                outcomes = json.loads(m.get("outcomes", "[]"))
                                yes_idx = next((i for i, o in enumerate(outcomes) if o.lower() in ("yes", "up", "above")), -1)
                                no_idx = next((i for i, o in enumerate(outcomes) if o.lower() in ("no", "down", "below")), -1)

                                if yes_idx != -1 and no_idx != -1:
                                    # Parse prices
                                    prices = json.loads(m.get("outcomePrices", "[]"))
                                    up_price = round(float(prices[yes_idx]) * 100) if len(prices) > yes_idx else 50
                                    down_price = round(float(prices[no_idx]) * 100) if len(prices) > no_idx else 50

                                    # Parse token IDs
                                    tokens = json.loads(m.get("clobTokenIds", "[]"))
                                    if len(tokens) > 1:
                                        live_token_ids["up"] = tokens[yes_idx]
                                        live_token_ids["down"] = tokens[no_idx]
                                        with open("tokens.json", "w") as f:
                                            json.dump([live_token_ids["up"], live_token_ids["down"]], f)

                                    # Calculate time remaining (until next hour boundary)
                                    next_hour = (now_secs // 3600 + 1) * 3600
                                    time_remaining = next_hour - now_secs

                                    # Set market state for new cycle (reject stale/thin-book prices)
                                    up_clamped = max(1, min(99, up_price))
                                    dn_clamped = max(1, min(99, down_price))
                                    is_stale = (up_clamped <= 5 or dn_clamped <= 5 or up_clamped >= 95 or dn_clamped >= 95)
                                    market.is_live = True
                                    market.transitioning = False  # New market found — simulator can run again
                                    market.live_slug = slug
                                    if not is_stale:
                                        market.up_price = up_clamped
                                        market.down_price = dn_clamped
                                        market.prices_loaded = True
                                    market.cycle_duration = time_remaining
                                    market.cycle_end_time = time.time() + time_remaining
                                    market.history = []  # Clear old chart data

                                    print(f"[AUTO] Connected to NEW market: {slug}")
                                    print(f"[AUTO] UP: {up_price}¢ | DOWN: {down_price}¢ | Time: {time_remaining}s")
                                    found_new = True
                                    break
                        if found_new:
                            break
                except Exception as e:
                    print(f"[AUTO] Failed to fetch slug {slug}: {e}")
                    continue

            if not found_new:
                print(f"[AUTO] No new market found, retrying in 15s...")
            await asyncio.sleep(15)

        except Exception as e:
            print(f"[AUTO] Discovery error: {e}")
            await asyncio.sleep(15)

async def _poll_live_prices():
    """Poll CLOB order book for live price updates using official py-clob-client. ALL pricing from Polymarket only."""
    try:
        up_token = live_token_ids.get("up", "")
        down_token = live_token_ids.get("down", "")

        # Paper-trading mode (no token IDs): poll Gamma API for prices instead
        if not up_token or not down_token:
            slug = market.live_slug
            if not slug:
                return
            async with httpx.AsyncClient(timeout=8.0) as hclient:
                resp = await hclient.get(f"https://gamma-api.polymarket.com/events?slug={slug}")
            data = resp.json() if resp.status_code == 200 else []
            if data and len(data) > 0:
                for m in data[0].get("markets", []):
                    if not m.get("closed") and m.get("active"):
                        outcomes = json.loads(m.get("outcomes", "[]"))
                        prices   = json.loads(m.get("outcomePrices", "[]"))
                        yes_idx  = next((i for i, o in enumerate(outcomes) if o.lower() in ("yes","up","above")), -1)
                        no_idx   = next((i for i, o in enumerate(outcomes) if o.lower() in ("no","down","below")), -1)
                        if yes_idx != -1 and no_idx != -1 and len(prices) > max(yes_idx, no_idx):
                            up_val = max(1, min(99, round(float(prices[yes_idx]) * 100)))
                            dn_val = max(1, min(99, round(float(prices[no_idx])  * 100)))
                            is_stale = (up_val <= 5 or dn_val <= 5 or up_val >= 95 or dn_val >= 95)
                            if not is_stale:
                                market.up_price   = up_val
                                market.down_price = dn_val
                                market.prices_loaded = True
                            market.current_price = market.up_price
                            now = time.time()
                            market.history.append((now, market.up_price))
                            market.history = [h for h in market.history if now - h[0] <= 300]
                            print(f"[GAMMA] UP={market.up_price}¢ DOWN={market.down_price}¢")
                        break
            return

        # Use /midpoint for real-time prices (more current than last-trade-price)
        async with httpx.AsyncClient(timeout=5.0) as hclient:
            up_resp = await hclient.get(f"https://clob.polymarket.com/midpoint?token_id={up_token}")
            dn_resp = await hclient.get(f"https://clob.polymarket.com/midpoint?token_id={down_token}")

        up_data = up_resp.json() if up_resp.status_code == 200 else {}
        dn_data = dn_resp.json() if dn_resp.status_code == 200 else {}

        # CLOB /midpoint returns {"mid": "0.41"}, not "price"
        up_raw = up_data.get("mid") or up_data.get("price")
        dn_raw = dn_data.get("mid") or dn_data.get("price")
        if up_raw:
            up_val = max(1, min(99, round(float(up_raw) * 100)))
            dn_val = max(1, min(99, round(float(dn_raw) * 100))) if dn_raw else market.down_price
            # Reject stale/thin-book prices: expired or illiquid new market
            is_stale = (up_val <= 5 or dn_val <= 5 or up_val >= 95 or dn_val >= 95)
            if not is_stale:
                market.up_price = up_val
                if dn_raw:
                    market.down_price = dn_val
                market.prices_loaded = True

        # Derive current_price from Polymarket token prices (up_price as probability)
        market.current_price = market.up_price
        now = time.time()
        market.history.append((now, market.up_price))
        market.history = [h for h in market.history if now - h[0] <= 300]

        print(f"[POLY] UP={market.up_price}¢ DOWN={market.down_price}¢")

    except Exception as e:
        print(f"[AUTO] Price poll error: {e}")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(market_loop())
    asyncio.create_task(auto_discover_market())
    asyncio.create_task(stream_live_prices())
    asyncio.create_task(sync_live_balance())

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            cmd = json.loads(data)
            action = cmd.get("action")
            
            global portfolio, active_strategy_a, active_strategy_b, active_strategy_c, active_strategy_c_trailing, active_strategy_d, active_strategy_e, active_strategy_f, active_strategy_7, active_strategy_cpt, active_strategy_claude, strategy_a_strikes, global_profit_target, live_mode_enabled, live_token_ids, strategy_stats
            current_portfolio = live_portfolio if live_mode_enabled else paper_portfolio
            portfolio = current_portfolio
            if action == "BUY_UP":
                shares = cmd.get("shares", 1)
                current_portfolio.buy('up', shares, market.up_price)
                if live_mode_enabled:
                    asyncio.create_task(execute_live_buy('up', shares, market.up_price))
            elif action == "BUY_DOWN":
                shares = cmd.get("shares", 1)
                current_portfolio.buy('down', shares, market.down_price)
                if live_mode_enabled:
                    asyncio.create_task(execute_live_buy('down', shares, market.down_price))
            elif action == "CASH_OUT":
                if live_mode_enabled:
                    if current_portfolio.positions['up'] > 0:
                        asyncio.create_task(execute_live_sell('up', current_portfolio.positions['up'], market.up_price))
                    if current_portfolio.positions['down'] > 0:
                        asyncio.create_task(execute_live_sell('down', current_portfolio.positions['down'], market.down_price))
                paper_portfolio.cash_out(market.up_price, market.down_price)
                live_portfolio.cash_out(market.up_price, market.down_price)
                market.peak_profit = 0.0
                strategy_a_strikes = 0
                strategy_b_trade_done = False
                scalper_4.reset_state()
                scalper_5.reset_state()
                scalper_6.reset_state()
                claude_strategy.reset_state(full_reset=True)
            elif action == "SET_VOLATILITY":
                market.volatility = float(cmd.get("value", 0.002))
            elif action == "CHANGE_TIMEFRAME":
                seconds = float(cmd.get("seconds", 3600))
                market.cycle_duration = seconds
                paper_portfolio.cash_out(market.up_price, market.down_price)
                live_portfolio.cash_out(market.up_price, market.down_price)
                paper_portfolio.cycle_profit = 0.0
                live_portfolio.cycle_profit = 0.0
                market.peak_profit = 0.0
                strategy_a_strikes = 0
                strategy_b_trade_done = False
                scalper_4.reset_state()
                scalper_5.reset_state()
                scalper_6.reset_state()
                claude_strategy.reset_state(full_reset=True)
                market.reset_cycle(market.current_price)
            elif action == "CONNECT_GAMMA":
                slug = cmd.get("slug", "").strip()
                if slug:
                    market.is_live = True
                    market.live_slug = slug
                    market.prices_loaded = False  # Wait for real prices before trading
                    paper_portfolio.cash_out(market.up_price, market.down_price)
                    live_portfolio.cash_out(market.up_price, market.down_price)
            elif action == "SYNC_LIVE_GAMMA":
                market.up_price = cmd.get("up_price", 50)
                market.down_price = cmd.get("down_price", 50)
                title = cmd.get("title", "Live Market")
                
                target_p = cmd.get("target_price", 0)
                if (target_p > 0):
                    market.price_to_beat = target_p
                    
                time_rem = cmd.get("time_remaining", -1)
                if (time_rem >= 0):
                    market.cycle_duration = time_rem
                    market.cycle_end_time = time.time() + time_rem
            elif action == "SET_PROFIT_TARGET":
                val = cmd.get("value")
                if val == "unlimited" or val == -1:
                    global_profit_target = float('inf')
                else:
                    global_profit_target = float(val)
                save_settings()
            elif action == "TOGGLE_STRATEGY_A":
                active_strategy_a = not active_strategy_a
                if active_strategy_a:
                    active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_7 = active_strategy_cpt = active_strategy_claude = False
                save_settings()
            elif action == "TOGGLE_STRATEGY_B":
                active_strategy_b = not active_strategy_b
                if active_strategy_b:
                    active_strategy_a = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_7 = active_strategy_cpt = active_strategy_claude = False
                save_settings()
            elif action == "TOGGLE_STRATEGY_C":
                active_strategy_c = not active_strategy_c
                if active_strategy_c:
                    paper_portfolio.cycle_profit = 0.0
                    live_portfolio.cycle_profit = 0.0
                    active_strategy_a = active_strategy_b = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_7 = active_strategy_cpt = active_strategy_claude = False
                save_settings()
            elif action == "TOGGLE_STRATEGY_C_TRAILING":
                active_strategy_c_trailing = not active_strategy_c_trailing
                if active_strategy_c_trailing:
                    paper_portfolio.cycle_profit = 0.0
                    live_portfolio.cycle_profit = 0.0
                    market.peak_profit = 0.0
                    active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_7 = active_strategy_cpt = active_strategy_claude = False
                save_settings()
            elif action == "TOGGLE_STRATEGY_D":
                active_strategy_d = not active_strategy_d
                if active_strategy_d:
                    active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_e = active_strategy_f = active_strategy_7 = active_strategy_cpt = active_strategy_claude = False
                save_settings()
            elif action == "TOGGLE_STRATEGY_E":
                active_strategy_e = not active_strategy_e
                if active_strategy_e:
                    active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_f = active_strategy_7 = active_strategy_cpt = active_strategy_claude = False
                save_settings()
            elif action == "TOGGLE_STRATEGY_F":
                active_strategy_f = not active_strategy_f
                if active_strategy_f:
                    active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_7 = active_strategy_cpt = active_strategy_claude = False
                save_settings()
            elif action == "TOGGLE_STRATEGY_7":
                active_strategy_7 = not active_strategy_7
                if active_strategy_7:
                    paper_portfolio.cycle_profit = 0.0
                    live_portfolio.cycle_profit = 0.0
                    active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_cpt = active_strategy_claude = False
                save_settings()
            elif action == "TOGGLE_STRATEGY_CPT":
                active_strategy_cpt = not active_strategy_cpt
                if active_strategy_cpt:
                    paper_portfolio.cycle_profit = 0.0
                    live_portfolio.cycle_profit = 0.0
                    market.peak_profit = 0.0
                    active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_7 = active_strategy_claude = False
                save_settings()
            elif action == "TOGGLE_STRATEGY_CLAUDE":
                active_strategy_claude = not active_strategy_claude
                if active_strategy_claude:
                    paper_portfolio.cycle_profit = 0.0
                    live_portfolio.cycle_profit = 0.0
                    market.peak_profit = 0.0
                    claude_strategy.reset_state(full_reset=True)
                    active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_7 = active_strategy_cpt = False
                save_settings()
            elif action == "TOGGLE_LIVE_MODE":
                if LIVE_TRADING_AVAILABLE:
                    live_mode_enabled = not live_mode_enabled
                    print(f"[LIVE] Trading mode: {'LIVE 🔴' if live_mode_enabled else 'PAPER 📝'}")
                else:
                    live_mode_enabled = False
                    print("[LIVE] Cannot enable live mode — CLOB client not available.")
            elif action == "SET_LIVE_TOKENS":
                up_token = cmd.get("up_token", "")
                down_token = cmd.get("down_token", "")
                if up_token:
                    live_token_ids["up"] = up_token
                if down_token:
                    live_token_ids["down"] = down_token
                with open("tokens.json", "w") as f:
                    json.dump([live_token_ids["up"], live_token_ids["down"]], f)
                print(f"[LIVE] Token IDs updated: UP={up_token[:20]}... DOWN={down_token[:20]}...")
            elif action == "RESET_STATS":
                strategy_stats = {k: {"trades":0,"wins":0,"total_profit":0.0,"best":0.0,"worst":0.0} for k in _stat_keys}
            elif action == "CLEAR_STRATEGIES":
                active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_7 = active_strategy_cpt = active_strategy_claude = False
                
    except WebSocketDisconnect:
        clients.remove(websocket)
    except Exception as e:
        print("WS Error:", e)

# Serve frontend static files
_frontend_dist = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'frontend', 'dist')
if os.path.exists(_frontend_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(_frontend_dist, "assets")), name="static")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        file_path = os.path.join(_frontend_dist, full_path)
        if full_path and os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(_frontend_dist, "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
