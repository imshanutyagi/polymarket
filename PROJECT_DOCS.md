# Polymarket BTC Trading Bot - Complete Project Documentation

## Overview

Automated BTC binary options trading bot for Polymarket's hourly "Bitcoin Up or Down" markets. Features multiple AI-powered strategies, real-time CLOB integration, paper/live trading modes, and a React dashboard.

**Repo:** https://github.com/imshanutyagi/polymarket.git
**Server:** Google Cloud Ubuntu VM (user: `chinkityagiji`, IP: `34.147.89.105`, project: `~/polymarket`)
**Deploy:** Run `~/deploy.sh` on server (pulls code + restarts bot in screen session)

---

## Architecture

```
Frontend (React/TypeScript)  <--WebSocket-->  Backend (Python/FastAPI)
     |                                              |
     |  Sends: BUY, SELL, TOGGLE, CONFIG            |  Talks to:
     |  Receives: state_update every 50ms           |  - Polymarket CLOB API (orderbook, trades)
     |                                              |  - Polymarket Gamma API (market discovery)
     |                                              |  - Binance API (real-time BTC price)
     |                                              |  - Claude/Gemini API (AI signals)
```

---

## File Structure

```
backend/
  main.py              # FastAPI server (~3100 lines) - ALL backend logic
  strategies.py         # Strategy classes (~1300 lines)
  requirements.txt      # Python deps: fastapi, uvicorn, httpx, py-clob-client
  .env                  # Live trading credentials (gitignored)
  settings.json         # Persisted strategy toggles + AI config
  tokens.json           # Current market token IDs
  test_prices.py        # Diagnostic: compare CLOB/Gamma/midpoint prices

frontend/
  src/App.tsx           # Main React component (~1900 lines)
  src/main.tsx          # React entry point
  vite.config.ts        # Vite bundler config
  package.json          # React deps: lightweight-charts, lucide-react, ws
```

---

## How the Bot Works (Core Loop)

### 1. Market Discovery (`auto_discover_market()` - main.py:2430)
- Runs continuously in background
- Generates slug like `bitcoin-up-or-down-march-16-2026-10am-et`
- Queries Gamma API: `https://gamma-api.polymarket.com/events?slug={slug}`
- Extracts: token IDs, outcome prices, market metadata
- **Pre-fetches next market 90 seconds before cycle ends** for instant transition
- Sets `market.is_live = True`, `market.prices_loaded = True`

### 2. Price Polling (`_poll_live_prices()` - main.py:2636)
- Polls CLOB every 1 second via `stream_live_prices()`
- Fetches both orderbooks: `GET https://clob.polymarket.com/book?token_id={TOKEN}`
- **Cross-book pricing** (matches Polymarket UI):
  - `Buy UP = min(UP_best_ask, 1 - DOWN_best_bid)`
  - `Buy DOWN = min(DOWN_best_ask, 1 - UP_best_bid)`
- Extracts: liquidity, spread, bid/ask depth
- Also fetches BTC price from Binance for NCAIO strategy
- If CLOB thin, falls back to Gamma `outcomePrices`

### 3. Strategy Execution (`market_loop()` - main.py:1671)
- Runs at 50ms tick interval (20x/second)
- Checks cycle status, resolves expired cycles
- Monitors positions: P&L, stop-loss, profit targets
- Executes enabled strategies: each `on_tick()` returns actions
- Actions: `buy_up`, `buy_down`, `sell_up`, `sell_down`, `cash_out`

### 4. AI Agent (`run_ai_agent()` - main.py:1402)
- State machine: idle → observing (2 min) → trading → holding → rescanning
- Calls Claude/Gemini API every 30 seconds for BUY_UP/BUY_DOWN/WAIT signal
- CLOB Safe Mode: auto-exits if orderbook goes stale >60s while holding
- Configurable: confidence threshold, max entry price, max spread

### 5. State Broadcasting (`broadcast_state()` - main.py:1541)
- Sends full state to all WebSocket clients every 50ms
- Includes: market data, portfolio, strategy states, AI agent status

---

## Strategies

### Strategy A-F + 7 (Simple Scalpers)
- Basic buy/sell strategies with different entry/exit rules
- Only one A-F can be active at a time
- Toggleable via frontend

### Strategy C+ Trail (`strategy_cpt`)
- Trailing stop variant of Strategy C
- Tracks peak profit, exits on pullback

### Claude AI Confluence Strategy (`ClaudeStrategy` - strategies.py:253)
- AI-powered with technical indicators (EMA, RSI, velocity)
- Strict risk management: max $20 position, -$8 max drawdown
- Profit zones: $0.90-$1.50 instant sell, $1.50+ trailing stop
- Entry only at 40-55 cents, 4 phases based on cycle time
- 3-minute observation period at cycle start

### New Claude All-in-One (`NewClaudeAllInOneStrategy` - strategies.py:788)
- Multi-trade approach: multiple $5-12 positions per cycle
- BTC price signal (40%), EMA crossover (30%), technicals (20%), conditions (10%)
- Smart profit mode: time-based targets ($0.50-$1.00 per trade)
- Position flipping on stop-loss + reversal confirmation
- AI confirmation option (Claude/Gemini) before entries
- Recovery mode: adjusts sizing after consecutive losses
- Choppy market detection (6+ reversals in 2 min → don't enter)

---

## Key Data Flow

### MarketState (main.py:559)
```python
market.up_price          # Current UP token price (1-99 cents)
market.down_price        # Current DOWN token price (1-99 cents)
market.price_to_beat     # BTC target price for this cycle
market.btc_price         # Real-time BTC price from Binance
market.is_live           # Connected to live Polymarket market
market.prices_loaded     # CLOB prices confirmed (blocks trading if False)
market.live_slug         # e.g. "bitcoin-up-or-down-march-16-2026-10am-et"
market.cycle_end_time    # Unix timestamp when cycle expires
market.history           # [(timestamp, up_price), ...] last 5 minutes
```

### Portfolio (main.py:635)
```python
portfolio.balance        # Current cash
portfolio.positions      # {'up': shares, 'down': shares}
portfolio.spent          # {'up': cost, 'down': cost}
portfolio.cycle_profit   # P&L for current cycle
portfolio.history        # Last 50 trades
```

### Token IDs (main.py:877)
```python
live_token_ids = {"up": "31436972...", "down": "10823313..."}
# Saved to tokens.json, loaded on restart
# Each hourly market has unique token IDs
```

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/ws` | WebSocket | Real-time state + commands |
| `/api/login` | POST | Auth with username/password |
| `/api/verify` | GET | Verify auth token |
| `/api/debug-prices` | GET | Compare all price sources (CLOB, Gamma, midpoint) |
| `/api/backtest` | POST | Run N-cycle backtest simulation |
| `/proxy/clob/{token_id}` | GET | Proxy to CLOB orderbook |
| `/proxy/gamma/` | GET | Proxy to Gamma market data |

---

## WebSocket Commands (Frontend → Backend)

```javascript
{ action: 'BUY_UP', shares: 10 }
{ action: 'BUY_DOWN', shares: 15 }
{ action: 'CASH_OUT' }
{ action: 'CONNECT_GAMMA', slug: 'bitcoin-up-or-down-...' }
{ action: 'SYNC_LIVE_GAMMA', up_price: 48, down_price: 52, target_price: 73500 }
{ action: 'TOGGLE_STRATEGY_CLAUDE' }
{ action: 'TOGGLE_STRATEGY_NCAIO' }
{ action: 'SET_AI_CONFIDENCE_THRESHOLD', value: 55 }
{ action: 'SET_AI_MAX_ENTRY', value: 70 }
{ action: 'SET_PROFIT_TARGET', value: 4.0 }
{ action: 'TOGGLE_LIVE_MODE' }
{ action: 'SET_NCAIO_CONFIG', trade_size: 'medium', risk_level: 'balanced', ... }
{ action: 'TEST_AI' }            // Test AI API key
{ action: 'TEST_NCAIO_AI' }     // Test NCAIO AI key
```

---

## External APIs Used

### Polymarket CLOB API (`https://clob.polymarket.com`)
- `GET /book?token_id={ID}` → Full orderbook (asks + bids)
- `GET /price?token_id={ID}&side=BUY` → Computed buy price
- `GET /midpoint?token_id={ID}` → Bid-ask midpoint
- `POST /order` → Place limit order (via py-clob-client)

### Polymarket Gamma API (`https://gamma-api.polymarket.com`)
- `GET /events?slug={slug}` → Market discovery, token IDs, display prices

### Binance API (`https://api.binance.com`)
- `GET /api/v3/ticker/price?symbol=BTCUSDT` → Real-time BTC price

### AI APIs
- Claude: `https://api.anthropic.com/v1/messages`
- Gemini: `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`

---

## Live Trading

### Requirements
- `.env` file with Polymarket credentials (API key, secret, passphrase, private key, proxy address)
- Toggle "LIVE" mode in frontend (confirmation dialog)
- Bot uses py-clob-client to place GTC limit orders

### Order Execution (main.py:906)
- Tries signature types 0, 1, 2 in order (EOA, Proxy, Proxy+funder)
- All orders are limit GTC (Good Till Cancelled)
- `execute_live_buy(direction, shares, price_cents)` → Places buy order
- `execute_live_sell(direction, shares, price_cents)` → Places sell order
- `cancel_all_open_orders()` → Cancels all pending orders on cycle transition

---

## Settings Persistence (main.py:785)

Saved to `settings.json`:
- All strategy toggle states
- AI agent config (model, threshold, max entry, safe mode, prompt mode)
- NCAIO config (trade size, risk, profit mode, stop loss, AI confirm)
- Strategy performance stats
- Global profit target

Loaded automatically on startup via `load_settings()`.

---

## Server Deployment

```bash
# Connect to server
ssh chinkityagiji@34.147.89.105

# Deploy (preferred method - uses ~/deploy.sh)
~/deploy.sh

# Manual deploy
cd ~/polymarket && git pull
screen -X -S polymarket quit
cd backend && screen -dmS polymarket python3 -m uvicorn main:app --host 0.0.0.0 --port 8000

# Check bot is running
screen -ls

# View logs
screen -r polymarket
# (Close terminal to detach - Ctrl+C will KILL the bot)
```

**IMPORTANT:** Never use `Ctrl+C` inside attached screen — it kills the bot. Just close the terminal window to detach safely.

---

## KNOWN ISSUES & BUGS

### 1. CLOB Pricing Stale at Cycle Start (CRITICAL - ONGOING)

**Problem:** When a new hourly market cycle begins, the CLOB REST API returns stale/default ~50/50 prices for the first 1-10 minutes. Meanwhile, Polymarket's website shows correct prices (e.g., 79/22 when BTC is well above target).

**Root Cause:** The Polymarket website likely uses a WebSocket or internal pricing engine that reflects market maker orders instantly. The REST `/book` endpoint lags behind, especially for newly created hourly markets. Both CLOB and Gamma APIs return stale data at cycle start.

**What We've Tried:**
1. Accept partial CLOB data (one side only) - didn't help, both sides stale
2. Use `/price` and `/midpoint` endpoints as fallback - these return worse data
3. Cross-book pricing (`Buy UP = min(UP_ask, 1 - DOWN_bid)`) - works mid-cycle but CLOB is empty at start
4. Stale detection via BTC direction + Gamma override - Gamma also stale
5. BTC-price-based probability calculation - user rejected (wants real Polymarket prices)
6. Pre-fetch next market 90 seconds before cycle end - helps with transition but CLOB still empty

**What Works:**
- Mid-cycle (10+ minutes in): Cross-book pricing matches Polymarket within 1-3 cents
- Pre-fetching: Bot transitions instantly to new market, no gap

**What Hasn't Been Tried:**
- **Polymarket WebSocket** (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) for real-time orderbook streaming - this is likely what the website uses
- Scraping the Polymarket website for prices directly
- Using a different undocumented API endpoint

**Reference Commit:** `7f6f2db` - User reports this version "didn't have the issue" (used simple `/book` best ask + Gamma fallback)

### 2. Screen Session Management
- `Ctrl+C` inside attached screen kills the bot process
- Workaround: use `screen -dmS polymarket ...` to start detached, never attach
- To restart: `screen -X -S polymarket quit` then start fresh

### 3. Git index.lock from OneDrive
- OneDrive syncing causes `.git/index.lock` conflicts
- Fix: `powershell -Command "Remove-Item -Force '.git/index.lock'"` then retry

---

## Pricing Architecture Detail

### How Polymarket Prices Work (Binary Markets)

In a binary market (UP/DOWN), each side has its own orderbook:
- **UP orderbook:** asks (sellers of UP) and bids (buyers of UP)
- **DOWN orderbook:** asks (sellers of DOWN) and bids (buyers of DOWN)

Because UP + DOWN = $1.00 (binary), buying UP is equivalent to selling DOWN:
- `Buy UP at 65c` = `Sell DOWN at 35c`
- `Buy DOWN at 36c` = `Sell UP at 64c`

**Polymarket's buy button shows the BEST available price across both methods:**
```
Buy UP  = min(UP_best_ask, 1 - DOWN_best_bid)
Buy DOWN = min(DOWN_best_ask, 1 - UP_best_bid)
```

This "cross-book" pricing is critical for accuracy. Using only direct asks gives wrong prices.

### Price Source Priority (Current)
1. CLOB `/book` with cross-book calculation (primary)
2. Gamma `outcomePrices` (fallback when CLOB thin)
3. Gamma prices from `auto_discover_market()` (initial on cycle start)

### The Cycle-Start Problem
- New market created each hour
- CLOB empty for first 1-10 minutes (no asks, no bids)
- Gamma returns default ~50/50
- Polymarket UI shows correct prices (uses internal engine?)
- Bot stuck at 50/50 until CLOB populates

---

## AI Signal Generation (main.py:970)

### Prompt Modes
- **Auto:** Picks mode based on market velocity
  - Fast (>=8c/min) → Classic
  - Medium (4-8c/min) → Balanced
  - Slow (<4c/min) → Smart
- **Classic:** Simple trend + direction, fast decisions
- **Balanced:** Technical + BTC price, moderate filter
- **Smart:** 5-signal checklist, high conviction only

### Signal Context Provided to AI
```
- Time remaining, UP/DOWN prices
- BTC price vs target (above/below and by how much)
- Price trend (rising/falling/flat)
- Trend consistency (strong/weak/choppy)
- Price velocity (cents/minute over 2min and 5min)
- Order book pressure (ask/bid ratio)
- Breakeven analysis
- Recent trade results
- Current balance and order size
```

### AI Response Format
```
SIGNAL:CONFIDENCE
BUY_UP:75    # Buy UP with 75% confidence
BUY_DOWN:60  # Buy DOWN with 60% confidence
WAIT:20      # Don't trade, only 20% confident
```

---

## Frontend UI Sections (App.tsx)

1. **Header:** Logo, timeframe selector (5M/15M/1H), Paper/Live toggle, balance
2. **Market Connection:** Slug display, Sync button, Auto-Pilot toggle
3. **Price Display:** UP/DOWN prices, target price, time remaining
4. **Market Prediction:** Buy UP/DOWN cards with current prices
5. **Live Market Chart:** Price history (lightweight-charts)
6. **Active Positions:** Current shares, cost, unrealized P&L
7. **Strategy Cards:** Toggle buttons for each strategy with descriptions
8. **Claude AI Agent Card:** Enable/disable, model selector, API key, confidence slider, safe mode
9. **NCAIO Strategy Card:** Trade size, risk level, profit mode, AI confirmation, stop loss
10. **Performance Stats:** Per-strategy win rate, total profit, trade count
11. **Backtest Panel:** Run simulations, view results table
12. **Trade History:** Last 50 trades with details

---

## Quick Reference

### Start Development
```bash
# Backend
cd backend && python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Frontend
cd frontend && npm run dev
```

### Key Globals (main.py)
```python
market              # MarketState instance
portfolio           # Current active portfolio (paper or live)
paper_portfolio     # Paper trading portfolio
live_portfolio      # Real trading portfolio
live_token_ids      # {"up": "...", "down": "..."}
clob_last_update_time  # Last successful CLOB price update
```

### Key Functions (main.py)
| Function | Line | Purpose |
|----------|------|---------|
| `auto_discover_market()` | 2430 | Find + connect to hourly market |
| `_poll_live_prices()` | 2636 | Fetch CLOB prices every 1s |
| `stream_live_prices()` | 2414 | Loop calling _poll_live_prices |
| `market_loop()` | 1671 | Main trading loop (50ms tick) |
| `run_ai_agent()` | 1402 | AI state machine |
| `get_ai_signal()` | 970 | Query Claude/Gemini for signal |
| `broadcast_state()` | 1541 | Send state to frontend |
| `execute_live_buy()` | 930+ | Place real buy order |
| `execute_live_sell()` | 950+ | Place real sell order |
| `save_settings()` | 785 | Persist config to disk |
| `load_settings()` | 824 | Load config from disk |
