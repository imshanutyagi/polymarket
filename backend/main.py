import asyncio
import json
import math
import os
import time
import traceback
from datetime import datetime
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Dict
from strategies import HighFrequencyScalper, TrendScaler

# --- Live Trading Setup ---
from dotenv import load_dotenv
import os
# Ensure it always finds the .env file exactly where main.py is, regardless of where uvicorn is launched
dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path)

LIVE_TRADING_AVAILABLE = False
clob_client = None
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds
    from py_clob_client.constants import POLYGON

    api_key = os.getenv("POLY_API_KEY", "")
    api_secret = os.getenv("POLY_API_SECRET", "")
    api_passphrase = os.getenv("POLY_API_PASSPHRASE", "")
    proxy_address = os.getenv("POLY_PROXY_ADDRESS", "")
    private_key = os.getenv("POLY_PRIVATE_KEY", "")

    if private_key and proxy_address:
        clob_client = ClobClient(
            "https://clob.polymarket.com",
            key=private_key,
            chain_id=POLYGON,
            signature_type=1,  # POLY_PROXY — standard Polymarket account
            funder=proxy_address
        )
        # Derive correct CLOB API credentials from private key (ignores Builder keys)
        creds = clob_client.create_or_derive_api_creds()
        clob_client.set_api_creds(creds)
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
            # keep only last 10
            if len(self.history) > 10:
                self.history = self.history[-10:]

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
            if len(self.history) > 10:
                self.history = self.history[-10:]

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
cpt_entry_time = 0.0  # Timestamp when C+ Trail position was opened
global_profit_target = 4.0
strategy_a_strikes = 0
strategy_a_done = False
strategy_b_trade_done = False
live_mode_enabled = os.getenv("LIVE_TRADING_ENABLED", "FALSE").upper() == "TRUE"  # Auto-set from .env
WARMUP_SECONDS = 120  # Wait 2 minutes before trading after start or new cycle
cycle_warmup_until = time.time() + WARMUP_SECONDS  # Initial startup warmup

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

async def execute_live_buy(direction: str, shares: float, price_cents: int):
    """Execute a real market buy on Polymarket CLOB."""
    if not clob_client or not live_mode_enabled:
        return
    try:
        token_id = live_token_ids.get(direction, "")
        if not token_id:
            print(f"[LIVE] No token ID for {direction} — skipping live order")
            return
        price = round(price_cents / 100.0, 2)
        order_args = OrderArgs(
            price=price,
            size=shares,
            side="BUY",
            token_id=token_id
        )
        signed_order = clob_client.create_order(order_args)
        resp = clob_client.post_order(signed_order, OrderType.GTC)
        print(f"[LIVE] BUY {shares} {direction.upper()} @ {price_cents}¢ → {resp}")
    except Exception as e:
        print(f"[LIVE] Buy order failed: {e}")

async def execute_live_sell(direction: str, shares: float, price_cents: int):
    """Execute a real market sell on Polymarket CLOB."""
    if not clob_client or not live_mode_enabled:
        return
    try:
        token_id = live_token_ids.get(direction, "")
        if not token_id:
            print(f"[LIVE] No token ID for {direction} — skipping live sell")
            return
        price = round(price_cents / 100.0, 2)
        order_args = OrderArgs(
            price=price,
            size=shares,
            side="SELL",
            token_id=token_id
        )
        signed_order = clob_client.create_order(order_args)
        resp = clob_client.post_order(signed_order, OrderType.GTC)
        print(f"[LIVE] SELL {shares} {direction.upper()} @ {price_cents}¢ → {resp}")
    except Exception as e:
        print(f"[LIVE] Sell order failed: {e}")

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
                "profit_target": global_profit_target if global_profit_target != float('inf') else -1,
                "strikes": strategy_a_strikes,
                "b_done": strategy_b_trade_done,
                "live_mode": live_mode_enabled,
                "live_available": LIVE_TRADING_AVAILABLE
            }
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
    global active_strategy_a, active_strategy_b
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
                cycle_warmup_until = time.time() + WARMUP_SECONDS  # Reset warmup for new cycle
                market.prices_loaded = False  # Force wait for fresh prices on new cycle
                print(f"[WARMUP] New cycle started. Trading paused for {WARMUP_SECONDS}s to allow price data to settle.")
                
            # Update dynamic pricing
            market.update_pricing()
            
            time_left = market.get_time_left_seconds()
            
            # Check for Early Profit Exit
            sunk = portfolio.total_spent
            if sunk > 0:
                up_val = portfolio.positions['up'] * (market.up_price / 100.0)
                down_val = portfolio.positions['down'] * (market.down_price / 100.0)
                live_profit = (up_val + down_val) - sunk
                
                if active_strategy_c_trailing or active_strategy_cpt:
                    # Time-Decaying Profit Target
                    dynamic_target = 1.0
                    if time_left <= 300: # 5 mins left
                        dynamic_target = 0.40
                    elif time_left <= 900: # 15 mins left
                        dynamic_target = 0.70
                        
                    market.peak_profit = max(live_profit, market.peak_profit)
                    
                    # 1. Immediate Target SELL: If it lands between dynamic target and $1.50, sell instantly. No waiting.
                    if dynamic_target <= live_profit <= 1.50:
                        portfolio.cash_out(market.up_price, market.down_price)
                        market.peak_profit = 0.0
                        strategy_a_strikes = 0
                        scalper_4.reset_state()
                        scalper_5.reset_state()
                        scalper_6.reset_state()
                        if active_strategy_a:
                            strategy_a_done = True
                        continue
                        
                    # 2. Trailing SELL: If it spiked > $1.50, we trail it. We only sell if it drops from its peak by a tight $0.15 buffer.
                    elif market.peak_profit > 1.50 and live_profit <= (market.peak_profit - 0.15):
                        portfolio.cash_out(market.up_price, market.down_price)
                        market.peak_profit = 0.0
                        strategy_a_strikes = 0
                        scalper_4.reset_state()
                        scalper_5.reset_state()
                        scalper_6.reset_state()
                        if active_strategy_a:
                            strategy_a_done = True
                        continue
                        
                    # 3. Panic SELL: Fallback in case peak_profit was >= dynamic target but it crashed completely below it
                    elif market.peak_profit >= dynamic_target and live_profit < dynamic_target:
                        portfolio.cash_out(market.up_price, market.down_price)
                        market.peak_profit = 0.0
                        strategy_a_strikes = 0
                        scalper_4.reset_state()
                        scalper_5.reset_state()
                        scalper_6.reset_state()
                        if active_strategy_a:
                            strategy_a_done = True
                        continue
                else:
                    # Original logic: Book profit at exactly $1.00 immediately for all other strategies
                    if live_profit >= 1.0:
                        portfolio.cash_out(market.up_price, market.down_price)
                        market.peak_profit = 0.0
                        strategy_a_strikes = 0
                        scalper_4.reset_state()
                        scalper_5.reset_state()
                        scalper_6.reset_state()
                        if active_strategy_a:
                            strategy_a_done = True
                        continue # Skip remainder of loop

            # Block all trading during warmup period OR until live prices are confirmed loaded
            if time.time() < cycle_warmup_until or (market.is_live and not market.prices_loaded):
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
                            # We're past the 40-min mark and losing $2+, cut it
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

            await broadcast_state()
            await asyncio.sleep(1)
        except Exception as e:
            print("Market Loop Error:", e)
            await asyncio.sleep(1)

async def auto_discover_market():
    """Automatically discover and sync with the current Polymarket BTC Up/Down market."""
    global live_token_ids
    
    while True:
        try:
            # Only auto-discover if we have CLOB client and no live market is connected yet,
            # OR if the current market expired (time_left <= 5)
            if not LIVE_TRADING_AVAILABLE:
                await asyncio.sleep(30)
                continue

            if market.is_live and market.get_time_left_seconds() > 10:
                # Already synced and market is active, just poll prices
                await _poll_live_prices()
                await asyncio.sleep(2)
                continue

            # --- Cycle expired or not yet connected: reset and rediscover ---
            old_slug = market.live_slug
            if market.is_live:
                print(f"[AUTO] Cycle ended for {old_slug}, discovering new market...")
                market.is_live = False
                market.live_slug = ""
                live_token_ids.clear()

            now_secs = int(time.time())
            from datetime import timezone, timedelta
            et_offset = timedelta(hours=-4)  # EDT (adjust to -5 for EST)

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

                                    # Set market state for new cycle
                                    market.is_live = True
                                    market.live_slug = slug
                                    market.up_price = max(1, min(99, up_price))
                                    market.down_price = max(1, min(99, down_price))
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
        if not up_token or not down_token:
            return

        loop = asyncio.get_event_loop()

        # Use the official py-clob-client (read-only works without auth)
        reader = clob_client if clob_client else None
        if reader is None:
            # Create a read-only client (no auth needed for prices)
            try:
                from py_clob_client.client import ClobClient as _Clob
                reader = _Clob("https://clob.polymarket.com")
            except Exception:
                return

        # get_price(token_id, side) returns a float string like "0.55"
        up_price_raw = await loop.run_in_executor(None, lambda: reader.get_last_trade_price(up_token))
        down_price_raw = await loop.run_in_executor(None, lambda: reader.get_last_trade_price(down_token))

        if up_price_raw and up_price_raw.get("price"):
            market.up_price = max(1, min(99, round(float(up_price_raw["price"]) * 100)))
            market.prices_loaded = True
        if down_price_raw and down_price_raw.get("price"):
            market.down_price = max(1, min(99, round(float(down_price_raw["price"]) * 100)))
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
            
            global portfolio, active_strategy_a, active_strategy_b, active_strategy_c, active_strategy_c_trailing, active_strategy_d, active_strategy_e, active_strategy_f, active_strategy_7, active_strategy_cpt, strategy_a_strikes, global_profit_target, live_mode_enabled, live_token_ids
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
            elif action == "TOGGLE_STRATEGY_A":
                active_strategy_a = not active_strategy_a
                if active_strategy_a: 
                    active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_7 = False
            elif action == "TOGGLE_STRATEGY_B":
                active_strategy_b = not active_strategy_b
                if active_strategy_b: 
                    active_strategy_a = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_7 = False
            elif action == "TOGGLE_STRATEGY_C":
                active_strategy_c = not active_strategy_c
                if active_strategy_c:
                    paper_portfolio.cycle_profit = 0.0 
                    live_portfolio.cycle_profit = 0.0 
                    active_strategy_a = active_strategy_b = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_7 = False
            elif action == "TOGGLE_STRATEGY_C_TRAILING":
                active_strategy_c_trailing = not active_strategy_c_trailing
                if active_strategy_c_trailing:
                    paper_portfolio.cycle_profit = 0.0
                    live_portfolio.cycle_profit = 0.0
                    market.peak_profit = 0.0
                    active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_7 = False
            elif action == "TOGGLE_STRATEGY_D":
                active_strategy_d = not active_strategy_d
                if active_strategy_d:
                    active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_e = active_strategy_f = active_strategy_7 = False
            elif action == "TOGGLE_STRATEGY_E":
                active_strategy_e = not active_strategy_e
                if active_strategy_e:
                    active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_f = active_strategy_7 = False
            elif action == "TOGGLE_STRATEGY_F":
                active_strategy_f = not active_strategy_f
                if active_strategy_f:
                    active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_7 = False
            elif action == "TOGGLE_STRATEGY_7":
                active_strategy_7 = not active_strategy_7
                if active_strategy_7:
                    paper_portfolio.cycle_profit = 0.0
                    live_portfolio.cycle_profit = 0.0
                    active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_cpt = False
            elif action == "TOGGLE_STRATEGY_CPT":
                active_strategy_cpt = not active_strategy_cpt
                if active_strategy_cpt:
                    paper_portfolio.cycle_profit = 0.0
                    live_portfolio.cycle_profit = 0.0
                    market.peak_profit = 0.0
                    active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_7 = False
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
            elif action == "CLEAR_STRATEGIES":
                active_strategy_a = active_strategy_b = active_strategy_c = active_strategy_c_trailing = active_strategy_d = active_strategy_e = active_strategy_f = active_strategy_7 = active_strategy_cpt = False
                
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
