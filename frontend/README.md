# Polymarket Trading Bot

## Strategy C+: Market Maker & Trailing Stop (Hybrid Trailing Strategy)

### Overview
Strategy C+ (internally known as Strategy C Trailing) is an intelligent, high-frequency "Market Maker Arbitrage" strategy designed to trap risk-free $1.00+ payouts by posting limit orders on both the UP and DOWN sides of a market. Once the strategy secures positions and crosses into profit, it uses a dynamically calculated **Hybrid Trailing Stop** to maximize payout yields.

### How It Works

1. **Market Entry & Trend Detection:**
   The bot continuously monitors the 4-minute pricing trend on the live Polymarket orderbook. It only places limit bid orders (priced at ≤ 45¢) when the trend aligns (or remains neutral) in a way that minimizes directional risk.

2. **Securing the Trade:**
   Due to its bid limits (≤ 45¢), if the bot successfully fills 30 shares of `UP` and 30 shares of `DOWN`, the maximum sunk cost is ~13.50¢. A winning side guarantees a $1.00 return per share ($30.00 total), meaning the profit buffer easily clears the $1.00 target *if both sides hit*.

3. **Hybrid Cash-Out Logic (The Profit Engine):**
   Once a live position generates profit, the bot no longer waits blindly for the market cycle to end. Instead, it aggressively tracks the `live_profit` based on live order book numbers.

4. **Intelligent 15-Minute Skew Guard (Risk Management):**
   If the total cycle profit target of **$4.00 is not hit before there are only 15 minutes left**, the bot will **NOT** take any new orders if the market conditions are heavily skewed (e.g., UP or DOWN is ≤ 20¢ / ≥ 80¢). Buying into heavily skewed odds directly before market expiration while underperforming is statistically dangerous, so the bot proactively halts trading during these high-risk conditions.

### The Recent Changes (What We Upgraded)

Previously, the bot would wait for a 1-cent drop to trigger a sell. However, because each point of price movement on 30+ shares equates to roughly $0.39+, a single down-tick could instantly wipe out a chunk of profit and drag the overall position below the $1.00 target threshold before it could sell.

**To solve this, we implemented the Hybrid Trailing Mechanism:**

* **1. The Danger Zone ($1.00 to $1.50) — Immediate Sell**
  When your profit is hovering tightly above the $1.00 baseline (between $1.00 and $1.50), the math dictates that a single 1-cent price drop could cost you $0.39+, risking your profit falling down to the $0.70 range. 
  **The Fix:** If the live profit lands anywhere in the $1.00 to $1.50 window, the bot **will sell immediately** (Instant Cash Out). It will *not* wait for any dips. This guarantees you lock in your $1.00+ target when the margin of error is too small.

* **2. The Trailing Zone (> $1.50) — Let It Ride**
  If a sudden market spike shoots your profit up quickly (bypassing the Danger Zone entirely and landing above $1.50 — like $1.70, $2.00, or $3.00), you now have enough "buffer" to endure a 1-cent dip without falling below $1.00.
  **The Fix:** The bot behaves as a true **Trailing Stop-Loss**. It will track the highest possible `peak_profit`. It will let the profit run as high as it possibly can. It will **only** cash out the *exact moment* the live profit ticks down from that peak.

* **3. The Panic Fallback (Crash Prevention)**
  If the profit peaked safely above $1.00, but a massive market swing crashes it back below $1.00 in a literal millisecond tick (skipping the $1.50 trailing line entirely), the bot will panic sell instantly to save every cent of positive profit remaining.

### Summary
* **Between $1.00 and $1.50:** Locks profit instantly.
* **Above $1.50:** Trails infinitely upwards; sells on the first dip.
* Result: You don't get "rug-pulled" below $1.00 on tiny profits, but you still capture massive spikes when the market breaks out.

---

## Strategy C+ Trail: Smart Hedge (Directional Scalp & Repeat)

### Overview
Strategy C+ Trail is an evolution of Strategy C+. Instead of buying both sides and waiting for a tiny arbitrage margin, it takes **directional bets** based on micro-trends and **scalps multiple quick trades** per cycle to reach the **$4.00 profit target**. It uses the same 3-zone exit system as C+ but adds intelligent time-based stop-loss management and a 10-minute timeout rule.

### Why It Was Created (The Problem with C+ for $4/Hour)
Strategy C+ buys both UP and DOWN sides equally. Since UP + DOWN prices always sum to ~100¢, the maximum profit per cycle is capped at **$0.30-$0.60** — mathematically impossible to reach the $4.00 target. Strategy C+ Trail solves this by going **directional** (one side only) and doing **multiple trades per cycle**.

---

### How It Works

#### 1. Entry Logic: Directional Scalp
- The bot reads the **1-minute micro trend** from BTC price history
- If BTC is trending **UP** in the last 60 seconds → Buy **30 UP shares** (only if price ≤ 50¢)
- If BTC is trending **DOWN** in the last 60 seconds → Buy **30 DOWN shares** (only if price ≤ 50¢)
- **Never buys both sides** — purely directional for maximum profit potential
- **Underdog Sniper:** If one side is super cheap (≤ 25¢), the bot takes the shot regardless of trend

#### 2. Scalp & Repeat Mechanism
- After each successful cash-out, positions reset to zero
- The bot **immediately scans** for the next micro-trend and re-enters
- This cycle repeats throughout the hour: **Enter → Profit → Exit → Re-enter**
- Trading stops when cumulative `cycle_profit` hits the **$4.00 target**

#### Example Hour:
| Trade # | Direction | Entry Cost | Exit Profit | Running Total |
|---------|-----------|-----------|-------------|---------------|
| #1 | UP at 42¢ | $12.60 | +$1.20 | $1.20 |
| #2 | DOWN at 38¢ | $11.40 | +$1.50 | $2.70 |
| #3 | UP at 45¢ | $13.50 | +$1.30 | $4.00 → **STOP** ✅ |

---

### Exit Logic: 3-Zone System (Inherited from C+)

#### Zone 1: The Danger Zone ($1.00 to $1.50) — Immediate Sell
When profit is between $1.00 and $1.50, a single 1-cent price drop could cost $0.39+, risking the profit falling below $1.00.
- **Action:** The bot **sells immediately** — no waiting, no trailing
- **Purpose:** Lock in the $1.00+ target when the margin of error is too small

#### Zone 2: The Trailing Zone (> $1.50) — Let It Ride
If profit spikes above $1.50 (like $1.70, $2.00, or $3.00), there's enough buffer to endure small dips.
- **Action:** The bot tracks the absolute `peak_profit` and **only sells when profit drops $0.15 from that peak**
- **Purpose:** Capture massive spikes while protecting gains with a tight trailing buffer

#### Zone 3: The Panic Fallback (Crash Prevention)
If profit peaked above $1.00 but crashes below it in a sudden market swing:
- **Action:** The bot **panic sells instantly** to save remaining positive profit
- **Purpose:** Prevent a profitable trade from turning into a loss

---

### Stop-Loss System: Time-Based Graduated Protection

The stop-loss adapts based on how much time is left in the cycle:

#### First 20 Minutes (60 → 40 min left): NO Stop-Loss
- **Reasoning:** BTC is volatile early in the cycle and frequently bounces back from dips
- If we bought UP at 48¢ and BTC dips temporarily, the price might drop to 35¢ (paper loss of -$3.90)
- But with 40+ minutes remaining, BTC almost always bounces back
- **Cutting too early locks in a loss that would have recovered into a profit**
- Let the trade breathe — time is on our side

#### Last 40 Minutes (40 → 0 min left): $2.00 Stop-Loss Active
- **Reasoning:** As time shrinks, BTC has less room to recover from a bad position
- If the position drops to -$2.00 or worse, the bot **exits immediately**
- This preserves capital for the next scalp entry
- Better to take a small -$2 loss than ride a -$8 disaster

---

### 10-Minute Timeout Rule

**Problem:** Sometimes the bot enters a trade and the market goes flat — the position sits at +$0.20 to +$0.50 for 10+ minutes without hitting the $1.00 target. This wastes valuable scalping time.

**Solution:** If the bot has been holding a position for **more than 10 minutes** and the position is in **any profit ≥ $0.10**, it automatically cashes out and re-enters on the next micro-trend.

- **Why $0.10 minimum?** We don't want to exit a position that is exactly breakeven or barely negative — that could trigger a pointless exit. We only exit if there's at least a dime of real profit to book.
- **Why 10 minutes?** On a 1-hour BTC market, if the price hasn't moved significantly in your direction within 10 minutes, the micro-trend that justified the entry has likely faded. Better to take the small win and try again.

---

### 15-Minute Skew Guard (Same as Strategy C+)

**Condition:** If ALL three are true, **no new trades** are placed:
1. ≤ 15 minutes left in the cycle (`time_left <= 900`)
2. Cycle profit is still under $4.00
3. Market is heavily skewed (one side ≤ 20¢ / other side ≥ 80¢)

**Reasoning:** Buying into heavily skewed odds in the last 15 minutes while underperforming is statistically dangerous. The bot proactively halts to protect existing gains.

**Exception:** If the market is NOT heavily skewed (both sides > 20¢), trading continues normally in the last 15 minutes.

---

### Complete Decision Flow

```
🔍 SCAN PHASE (Every 1 second):
│
├── Is cycle_profit >= $4.00? → STOP (target reached, no more trades this cycle)
│
├── Do we have an open position?
│   ├── YES → Check Exit Rules:
│   │   ├── 3-Zone Profit Exit (Danger/Trailing/Panic)
│   │   ├── 10-Min Timeout (holding >10min AND profit >= $0.10 → cash out)
│   │   └── Time-Based Stop-Loss (past 40-min mark AND loss >= -$2 → cut it)
│   │
│   └── NO → Check Entry Rules:
│       ├── Is time_left > 60 seconds? (don't enter in last minute)
│       ├── 15-Min Skew Guard passed?
│       ├── Micro trend detected? (not "neutral")
│       └── Price ≤ 50¢ on the trending side?
│           ├── All YES → BUY (directional, 30 shares)
│           └── Any NO → WAIT (scan again next second)
```

---

### Key Differences: C+ vs C+ Trail

| Feature | Strategy C+ | Strategy C+ Trail |
|---------|------------|-------------------|
| **Position Type** | Both sides (UP + DOWN) | ONE side only (directional) |
| **Entry Price** | ≤ 45¢ | ≤ 50¢ |
| **Trend Requirement** | Micro + Macro + 4min alignment | 1-minute micro trend only |
| **Max Profit Per Trade** | $0.30-$0.60 (capped by hedging) | $1.00-$5.00+ (uncapped) |
| **Trades Per Cycle** | 1 (sits in position) | 3-5+ (scalp & repeat) |
| **Stop-Loss** | None | Time-based ($2 after 40-min mark) |
| **10-Min Timeout** | None | Auto-cash-out if profit ≥ $0.10 |
| **Risk Level** | Very Low (guaranteed profit) | Medium (directional risk) |
| **$4/Hour Target** | ❌ Impossible mathematically | ✅ Achievable in 3-4 trades |

---

### Configuration
- **Profit Target:** $4.00 per cycle (configurable via dashboard)
- **Share Size:** 30 shares per trade
- **Entry Price Cap:** ≤ 50¢ (≤ 25¢ for underdog sniper)
- **Stop-Loss:** $2.00 (active after 40-minute mark only)
- **Timeout:** 10 minutes (cash out if holding > 10 min with ≥ $0.10 profit)
- **Skew Guard:** No new trades if ≤ 15 min left + profit < $4 + one side ≤ 20¢
