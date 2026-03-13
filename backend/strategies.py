from typing import Dict, Optional, Any, List
import time
import math

class HighFrequencyScalper:
    def __init__(self):
        # 1. Core Philosophy: Track internal pending limit states 
        self.pending_buy: Optional[Dict[str, Any]] = None   # format: {'side': 'up'/'down', 'price': 71, 'shares': 20}
        self.pending_sell: Optional[Dict[str, Any]] = None  # format: {'side': 'up'/'down', 'price': 76, 'shares': 20}
        
        # Track currently held executed shares
        self.held_positions = {'up': 0.0, 'down': 0.0}
        self.held_avg_cost = {'up': 0.0, 'down': 0.0}
        self.close_only_mode = False

    def reset_state(self):
        self.pending_buy = None
        self.pending_sell = None
        self.held_positions = {'up': 0, 'down': 0}
        self.held_avg_cost = {'up': 0.0, 'down': 0.0}
        self.close_only_mode = False

    def on_tick(self, time_remaining: float, up_price: float, down_price: float, portfolio: dict):
        """
        Main Event Loop for Strategy 4. Must be called every tick.
        """
        
        # 4. The "Time-Stop" Safety Protocol (Crucial - 2 minutes remaining)
        actions = []
        if time_remaining <= 120:
            if not self.close_only_mode:
                self.close_only_mode = True
                
                # Cancel all open Limit Buy and Limit Sell orders.
                self.pending_buy = None
                self.pending_sell = None
                
                # Market Sell held positions instantly to flatten the book.
                if self.held_positions['up'] > 0:
                    actions.append({
                        "action": "market_sell",
                        "side": "up",
                        "shares": self.held_positions['up'],
                        "price": up_price
                    })
                    self.held_positions['up'] = 0
                    self.held_avg_cost['up'] = 0.0
                    
                if self.held_positions['down'] > 0:
                    actions.append({
                        "action": "market_sell",
                        "side": "down",
                        "shares": self.held_positions['down'],
                        "price": down_price
                    })
                    self.held_positions['down'] = 0
                    self.held_avg_cost['down'] = 0.0
                    
            return actions # Return sell market payloads to main event loop
        
        # Process pending Limit ORDERS based on current tick price
        
        # --- EXECUTE LIMIT ASKS (Take Profit) ---
        if self.pending_sell is not None:
            side = str(self.pending_sell.get('side', ''))
            ask_price = float(self.pending_sell.get('price', 0.0))
            
            # If the market price crosses or equals our requested sell price
            if (side == 'up' and up_price >= ask_price) or (side == 'down' and down_price >= ask_price):
                trade_price = up_price if side == 'up' else down_price
                
                # Execute Sell
                actions.append({
                    "action": "limit_sell_fill",
                    "side": side,
                    "shares": self.pending_sell['shares'],
                    "price": trade_price
                })
                self.held_positions[side] -= self.pending_sell['shares']
                self.pending_sell = None
                
        # --- EXECUTE LIMIT BIDS (Entry) ---
        if self.pending_buy:
            side = self.pending_buy['side']
            bid_price = self.pending_buy['price']
            
            # If market price drops to or below our requested buy price
            if (side == 'up' and up_price <= bid_price) or (side == 'down' and down_price <= bid_price):
                trade_price = up_price if side == 'up' else down_price
                shares = self.pending_buy['shares']
                
                # Execute Buy
                actions.append({
                    "action": "limit_buy_fill",
                    "side": side,
                    "shares": shares,
                    "price": trade_price
                })
                
                # Update held state
                total_cost = (self.held_positions[side] * self.held_avg_cost[side]) + (shares * trade_price)
                self.held_positions[side] += shares
                self.held_avg_cost[side] = total_cost / self.held_positions[side]
                
                # We just filled! 
                # 3. The Exit Logic (The Ask): Immediately calculate Take-Profit
                self.pending_sell = {
                    'side': side,
                    'price': trade_price + 5, # 5¢ spread default
                    'shares': shares
                }
                self.pending_buy = None

        # --- 2. The Entry Logic (The Bid) ---
        # If we have NO open limit orders and NO held positions, look for entry
        if not self.pending_buy and not self.pending_sell and self.held_positions['up'] == 0 and self.held_positions['down'] == 0:
            if not self.close_only_mode:
                # Detect Momentum Spread (60 to 80 cents)
                if 60 <= up_price <= 80:
                    self.pending_buy = {
                        'side': 'up',
                        'price': up_price - 3, # 3¢ below current
                        'shares': 20.0
                    }
                elif 60 <= down_price <= 80:
                    self.pending_buy = {
                        'side': 'down',
                        'price': down_price - 3, # 3¢ below current
                        'shares': 20.0
                    }
        return actions

class TrendScaler:
    def __init__(self):
        self.held_positions = {'up': 0.0, 'down': 0.0}
        self.held_avg_cost = {'up': 0.0, 'down': 0.0}
        self.sell_targets = []
        self.close_only_mode = False

    def reset_state(self):
        self.held_positions = {'up': 0.0, 'down': 0.0}
        self.held_avg_cost = {'up': 0.0, 'down': 0.0}
        self.sell_targets = []
        self.close_only_mode = False

    def _execute_market_sells(self, up_price, down_price, actions):
        if self.held_positions['up'] > 0:
            actions.append({
                "action": "market_sell",
                "side": "up",
                "shares": self.held_positions['up'],
                "price": up_price
            })
            self.held_positions['up'] = 0.0
            self.held_avg_cost['up'] = 0.0
            
        if self.held_positions['down'] > 0:
            actions.append({
                "action": "market_sell",
                "side": "down",
                "shares": self.held_positions['down'],
                "price": down_price
            })
            self.held_positions['down'] = 0.0
            self.held_avg_cost['down'] = 0.0

    def on_tick(self, time_remaining: float, up_price: float, down_price: float, portfolio: dict):
        actions = []
        
        # 1. Time-Stop Safety Protocol
        if time_remaining <= 120:
            if not self.close_only_mode:
                self.close_only_mode = True
                self.sell_targets = [] # cancel all limit orders
                self._execute_market_sells(up_price, down_price, actions)
            return actions
            
        # 2. Execute Pending Sell Targets (Take Profit)
        still_pending = []
        for target in self.sell_targets:
            side = target['side']
            ask_price = target['price']
            shares = target['shares']
            
            # Check if market hit our limit sell price
            if (side == 'up' and up_price >= ask_price) or (side == 'down' and down_price >= ask_price):
                trade_price = up_price if side == 'up' else down_price
                
                # Double check we don't oversell
                shares_to_sell = min(shares, self.held_positions[side])
                if shares_to_sell > 0:
                    actions.append({
                        "action": "limit_sell_fill",
                        "side": side,
                        "shares": shares_to_sell,
                        "price": trade_price
                    })
                    self.held_positions[side] -= shares_to_sell
            else:
                still_pending.append(target)
                
        self.sell_targets = still_pending

        # 3. Momentum Ride Logic & 4. Convexity Hedge
        if not self.close_only_mode:
            prices = {'up': up_price, 'down': down_price}
            
            for side in ['up', 'down']:
                current_price = prices[side]
                other_side = 'down' if side == 'up' else 'up'
                
                # Rule 1: The Momentum Scale-In (70 to 80 cents)
                if 70 <= current_price <= 80 and self.held_positions[side] < 40:
                    buy_shares = 10.0
                    actions.append({
                        "action": "limit_buy_fill",
                        "side": side,
                        "shares": buy_shares,
                        "price": current_price
                    })
                    
                    total_cost = (self.held_positions[side] * self.held_avg_cost[side]) + (buy_shares * current_price)
                    self.held_positions[side] += buy_shares
                    self.held_avg_cost[side] = total_cost / self.held_positions[side]
                    
                    # Rule 2: Queue Profit Tiers (+5c and +10c)
                    self.sell_targets.append({'side': side, 'price': current_price + 5, 'shares': buy_shares / 2})
                    self.sell_targets.append({'side': side, 'price': current_price + 10, 'shares': buy_shares / 2})
                    break 
                
                # Rule 3: The Convexity Hedge
                if self.held_positions[other_side] >= 20:
                    if 5 <= current_price <= 12 and self.held_positions[side] < 20: 
                        hedge_shares = 5.0
                        actions.append({
                            "action": "limit_buy_fill",
                            "side": side,
                            "shares": hedge_shares,
                            "price": current_price
                        })
                        
                        total_cost = (self.held_positions[side] * self.held_avg_cost[side]) + (hedge_shares * current_price)
                        self.held_positions[side] += hedge_shares
                        self.held_avg_cost[side] = total_cost / self.held_positions[side]
                        
                        # Aggressive Take-Profit for the Hedge if it spikes to 40c
                        self.sell_targets.append({'side': side, 'price': 40, 'shares': hedge_shares})
                        break

        return actions


class ClaudeStrategy:
    """
    Claude Strategy: AI Confluence Trading Engine

    Uses EMA crossover, RSI, price velocity, and multi-timeframe trend confluence
    to make high-probability trades. Phase-aware with dynamic position sizing
    based on confidence scoring. Targets $4/hour with strict risk management.

    Market Intelligence:
    - BTC hourly markets tend to trend once direction establishes (first 15-20 min)
    - Mean reversion kicks in when one side reaches >75¢
    - Best entries are in the 40-55¢ zone with confirmed momentum
    - Late-cycle entries should only happen at deep discounts (<35¢)
    - UP + DOWN should sum ~100¢; deviations signal inefficiency
    - Rapid price moves (>5¢ in 30s) often overshoot then retrace 2-3¢
    """

    # === Constants ===
    MAX_POSITION_DOLLARS = 20.0
    MAX_DRAWDOWN = -8.0
    MIN_ENTRY_INTERVAL = 30.0      # seconds between entries (anti-whipsaw)
    MAX_ENTRY_PRICE = 55           # cents — never buy above this
    LATE_CYCLE_MAX_PRICE = 35      # cents — for last 15 min
    EMA_FAST_ALPHA = 2.0 / 11.0   # 10-tick EMA
    EMA_SLOW_ALPHA = 2.0 / 31.0   # 30-tick EMA
    RSI_PERIOD = 14

    # Zone-based profit taking
    ZONE1_MIN = 1.00              # $1.00 — immediate sell threshold
    ZONE1_MAX = 1.50              # $1.50 — upper bound of danger zone
    ZONE2_TRAILING_DROP = 0.15    # $0.15 drop from peak triggers sell in Zone 2
    PANIC_SELL_THRESHOLD = 1.00   # sell instantly if profit crashes below this after peaking above

    # Graduated stop-loss
    STOPLOSS_AMOUNT = -2.00       # $2.00 stop-loss in last 40 minutes
    STOPLOSS_GRACE_MINUTES = 20   # no stop-loss for first 20 minutes

    # 10-minute timeout
    TIMEOUT_MINUTES = 10          # max hold time before taking small profit
    TIMEOUT_MIN_PROFIT = 0.10     # minimum $0.10 profit to trigger timeout exit

    def __init__(self):
        self.held_positions = {'up': 0.0, 'down': 0.0}
        self.held_avg_cost = {'up': 0.0, 'down': 0.0}

        # Technical indicators
        self.ema_fast: Optional[float] = None
        self.ema_slow: Optional[float] = None
        self.rsi_prices: List[float] = []
        self.tick_count = 0

        # Strategy state
        self.last_entry_time = 0.0
        self.position_entry_time = 0.0   # when current position was opened
        self.cycle_drawdown = 0.0
        self.peak_unrealized = 0.0
        self.profit_ever_hit_target = False  # tracks if profit ever crossed $1.00
        self.confidence = 0
        self.phase = 1
        self.close_only_mode = False
        self.cycle_start_time = 0.0
        self.exit_reason = ""              # last exit reason for logging

    def reset_state(self):
        self.held_positions = {'up': 0.0, 'down': 0.0}
        self.held_avg_cost = {'up': 0.0, 'down': 0.0}
        self.ema_fast = None
        self.ema_slow = None
        self.rsi_prices = []
        self.tick_count = 0
        self.last_entry_time = 0.0
        self.position_entry_time = 0.0
        self.cycle_drawdown = 0.0
        self.peak_unrealized = 0.0
        self.profit_ever_hit_target = False
        self.confidence = 0
        self.phase = 1
        self.close_only_mode = False
        self.cycle_start_time = 0.0
        self.exit_reason = ""

    def _update_ema(self, price: float):
        """Update fast and slow exponential moving averages."""
        if self.ema_fast is None:
            self.ema_fast = price
            self.ema_slow = price
        else:
            self.ema_fast = self.EMA_FAST_ALPHA * price + (1 - self.EMA_FAST_ALPHA) * self.ema_fast
            self.ema_slow = self.EMA_SLOW_ALPHA * price + (1 - self.EMA_SLOW_ALPHA) * self.ema_slow

    def _compute_rsi(self, price: float) -> float:
        """Compute 14-period RSI. Returns 50.0 if not enough data."""
        self.rsi_prices.append(price)
        if len(self.rsi_prices) > self.RSI_PERIOD + 1:
            self.rsi_prices = self.rsi_prices[-(self.RSI_PERIOD + 1):]

        if len(self.rsi_prices) < 2:
            return 50.0

        gains = []
        losses = []
        for i in range(1, len(self.rsi_prices)):
            delta = self.rsi_prices[i] - self.rsi_prices[i - 1]
            if delta > 0:
                gains.append(delta)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(delta))

        avg_gain = sum(gains) / len(gains) if gains else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0

        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0

        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _compute_price_velocity(self, history: list, now: float) -> tuple:
        """
        Compute rate of price change over 30s and 60s windows.
        Returns (vel_30s, vel_60s) in cents per second.
        Positive = UP price increasing, Negative = UP price decreasing.
        """
        vel_30 = 0.0
        vel_60 = 0.0

        hist_30 = [h for h in history if now - h[0] <= 30]
        hist_60 = [h for h in history if now - h[0] <= 60]

        if len(hist_30) >= 2:
            dt = hist_30[-1][0] - hist_30[0][0]
            if dt > 0:
                vel_30 = (hist_30[-1][1] - hist_30[0][1]) / dt

        if len(hist_60) >= 2:
            dt = hist_60[-1][0] - hist_60[0][0]
            if dt > 0:
                vel_60 = (hist_60[-1][1] - hist_60[0][1]) / dt

        return vel_30, vel_60

    def _compute_trend_strength(self, history: list, now: float) -> int:
        """
        Composite trend strength score 0-100.
        Combines 1m micro, 4m standard, 15m macro trends + EMA alignment.
        Returns positive for UP trend, negative for DOWN trend (magnitude = strength).
        """
        score = 0
        direction_votes = 0  # positive = up, negative = down

        # 1-minute micro trend
        hist_1m = [h for h in history if now - h[0] <= 60]
        if len(hist_1m) >= 3:
            delta = hist_1m[-1][1] - hist_1m[0][1]
            if delta > 0:  # any upward move in 1 min
                score += 20
                direction_votes += 1
            elif delta < 0:
                score += 20
                direction_votes -= 1

        # 4-minute standard trend
        hist_4m = [h for h in history if now - h[0] <= 240]
        if len(hist_4m) >= 5:
            delta = hist_4m[-1][1] - hist_4m[0][1]
            if delta > 0:  # any move in 4 min
                score += 25
                direction_votes += 1
            elif delta < 0:
                score += 25
                direction_votes -= 1

        # 5-minute macro trend (full history window)
        if len(history) >= 10:
            delta = history[-1][1] - history[0][1]
            if delta > 1.0:  # >1¢ move over full window
                score += 25
                direction_votes += 1
            elif delta < -1.0:
                score += 25
                direction_votes -= 1

        # EMA alignment bonus
        if self.ema_fast is not None and self.ema_slow is not None:
            ema_diff = self.ema_fast - self.ema_slow
            if abs(ema_diff) > 0.1:  # any meaningful EMA divergence
                score += 15
                if ema_diff > 0:
                    direction_votes += 1
                else:
                    direction_votes -= 1

        # Consistency bonus: all indicators agree
        if abs(direction_votes) >= 2:
            score += 15

        # Cap at 100
        score = min(100, score)

        # Return signed score (positive = UP favored, negative = DOWN favored)
        if direction_votes > 0:
            return score
        elif direction_votes < 0:
            return -score
        return 0

    def _get_phase(self, time_left: float, cycle_duration: float) -> int:
        """Determine trading phase based on time remaining."""
        # Use time since Claude was activated (not cycle elapsed) so it always
        # observes for 5 min on first enable, even mid-cycle.
        claude_elapsed = time.time() - self.cycle_start_time if self.cycle_start_time > 0 else 0
        if claude_elapsed < 180:     # First 3 min since activation: observe only
            return 1
        elif time_left > 900:        # >15 min left: active trading
            return 2
        elif time_left > 300:        # 5-15 min left: high confidence only
            return 3
        else:                        # Last 5 min: exit only
            return 4

    def _has_position(self) -> bool:
        return self.held_positions['up'] > 0 or self.held_positions['down'] > 0

    def _get_unrealized_pnl(self, up_price: int, down_price: int) -> float:
        """Calculate unrealized P&L on held positions."""
        pnl = 0.0
        for side in ['up', 'down']:
            if self.held_positions[side] > 0:
                market_price = up_price if side == 'up' else down_price
                market_value = self.held_positions[side] * (market_price / 100.0)
                cost_basis = self.held_positions[side] * (self.held_avg_cost[side] / 100.0)
                pnl += market_value - cost_basis
        return pnl

    def on_tick(self, time_remaining: float, up_price: int, down_price: int,
                portfolio, history: list, cycle_profit: float = 0.0, profit_target: float = float('inf')) -> List[dict]:
        """
        Main tick handler. Called every ~1 second.
        Returns list of actions to execute.
        """
        actions = []
        now = time.time()

        # Initialize cycle start
        if self.cycle_start_time == 0.0:
            self.cycle_start_time = now

        self.tick_count += 1

        # Update technical indicators
        self._update_ema(float(up_price))
        rsi = self._compute_rsi(float(up_price))
        vel_30, vel_60 = self._compute_price_velocity(history, now)
        trend_strength = self._compute_trend_strength(history, now)

        # Determine phase
        cycle_duration = 3600.0  # 1 hour default
        self.phase = self._get_phase(time_remaining, cycle_duration)

        # === TIME-STOP: 2 minutes left, market sell everything ===
        if time_remaining <= 120:
            if not self.close_only_mode:
                self.close_only_mode = True
                for side in ['up', 'down']:
                    if self.held_positions[side] > 0:
                        price = up_price if side == 'up' else down_price
                        actions.append({
                            "action": "market_sell",
                            "side": side,
                            "shares": self.held_positions[side],
                            "price": price
                        })
                        self.held_positions[side] = 0.0
                        self.held_avg_cost[side] = 0.0
                self.exit_reason = "time_stop_2min"
                self.peak_unrealized = 0.0
                self.profit_ever_hit_target = False
                self.position_entry_time = 0.0
            return actions

        # === EXIT LOGIC: 3-Zone Profit System + Graduated Stop-Loss + 10-Min Timeout ===
        if self._has_position():
            unrealized = self._get_unrealized_pnl(up_price, down_price)
            self.peak_unrealized = max(self.peak_unrealized, unrealized)

            # Track if profit ever crossed $1.00
            if self.peak_unrealized >= self.ZONE1_MIN:
                self.profit_ever_hit_target = True

            should_exit = False
            exit_reason = ""

            # Time since position was opened
            hold_time = now - self.position_entry_time if self.position_entry_time > 0 else 0
            # Time elapsed in cycle (simple: 3600 - time_remaining)
            cycle_elapsed = 3600.0 - time_remaining
            cycle_elapsed_minutes = cycle_elapsed / 60.0

            # --- Phase 4: exit everything (last 5 min) ---
            if self.phase == 4:
                should_exit = True
                exit_reason = "phase4_exit"

            # --- ZONE 3: Panic Fallback ---
            # If profit peaked above $1.00 but crashed below it → panic sell instantly
            elif self.profit_ever_hit_target and unrealized < self.PANIC_SELL_THRESHOLD:
                should_exit = True
                exit_reason = f"zone3_panic_sell (peaked ${self.peak_unrealized:.2f}, now ${unrealized:.2f})"

            # --- ZONE 1: $1.00–$1.50 — Instant Sell ---
            # Sell immediately when profit lands in this range (same as C+Trailing)
            elif self.ZONE1_MIN <= unrealized <= self.ZONE1_MAX:
                should_exit = True
                exit_reason = f"zone1_instant_sell (${unrealized:.2f})"

            # --- ZONE 2: > $1.50 — Trailing Stop ---
            # Let it ride above $1.50, only exit when profit drops $0.15 from peak
            elif unrealized > self.ZONE1_MAX:
                if self.peak_unrealized - unrealized >= self.ZONE2_TRAILING_DROP:
                    should_exit = True
                    exit_reason = f"zone2_trailing_sell (peak ${self.peak_unrealized:.2f}, now ${unrealized:.2f})"
                # else: let it ride

            # --- Graduated Stop-Loss ---
            # First 20 minutes: NO stop-loss (BTC is volatile, let it breathe)
            # Last 40 minutes: $2.00 stop-loss active
            elif cycle_elapsed_minutes > self.STOPLOSS_GRACE_MINUTES and unrealized <= self.STOPLOSS_AMOUNT:
                should_exit = True
                exit_reason = f"graduated_stoploss (${unrealized:.2f}, {cycle_elapsed_minutes:.0f}min elapsed)"

            # --- 10-Minute Timeout Rule ---
            # Only apply timeout for small-profit positions (< $0.80). High-profit positions
            # are managed by the trailing stop above — don't force-close them on a timer.
            elif hold_time >= self.TIMEOUT_MINUTES * 60 and self.TIMEOUT_MIN_PROFIT <= unrealized < 0.80:
                should_exit = True
                exit_reason = f"10min_timeout (${unrealized:.2f} after {hold_time/60:.1f}min)"

            if should_exit:
                self.exit_reason = exit_reason
                for side in ['up', 'down']:
                    if self.held_positions[side] > 0:
                        price = up_price if side == 'up' else down_price
                        actions.append({
                            "action": "market_sell",
                            "side": side,
                            "shares": self.held_positions[side],
                            "price": price
                        })
                        self.held_positions[side] = 0.0
                        self.held_avg_cost[side] = 0.0
                self.peak_unrealized = 0.0
                self.profit_ever_hit_target = False
                self.position_entry_time = 0.0
                return actions

        # === ENTRY LOGIC ===
        if not self._has_position() and not self.close_only_mode:

            # Guard: cycle profit target already reached — STOP trading
            if cycle_profit >= profit_target:
                self.confidence = 0
                return actions

            # Guard: max drawdown hit
            if self.cycle_drawdown <= self.MAX_DRAWDOWN:
                return actions

            # Guard: phase 1 = observation only (build indicator baselines)
            if self.phase == 1:
                # Still compute confidence for UI display
                self.confidence = abs(trend_strength)
                return actions

            # Guard: anti-whipsaw cooldown
            if now - self.last_entry_time < self.MIN_ENTRY_INTERVAL:
                return actions

            # Determine favored side from EMA crossover
            if self.ema_fast is None or self.ema_slow is None:
                return actions

            ema_diff = self.ema_fast - self.ema_slow
            if abs(ema_diff) < 0.05:
                # EMAs too close — no clear signal
                self.confidence = max(0, abs(trend_strength) - 10)
                return actions

            favored_side = 'up' if ema_diff > 0 else 'down'
            favored_price = up_price if favored_side == 'up' else down_price

            # === Confluence Check: need 2+ of 5 signals ===
            signals = 0

            # Signal 1: EMA crossover direction
            if (favored_side == 'up' and ema_diff > 0.1) or \
               (favored_side == 'down' and ema_diff < -0.1):
                signals += 1

            # Signal 2: RSI not overbought/oversold against entry
            if favored_side == 'up' and rsi < 70:
                signals += 1
            elif favored_side == 'down' and rsi > 30:
                signals += 1

            # Signal 3: Trend strength confirms direction
            if (favored_side == 'up' and trend_strength > 15) or \
               (favored_side == 'down' and trend_strength < -15):
                signals += 1

            # Signal 4: 30s velocity confirms direction
            if (favored_side == 'up' and vel_30 > 0.01) or \
               (favored_side == 'down' and vel_30 < -0.01):
                signals += 1

            # Signal 5: 60s velocity confirms (macro momentum)
            if (favored_side == 'up' and vel_60 > 0.005) or \
               (favored_side == 'down' and vel_60 < -0.005):
                signals += 1

            # Compute confidence (0-100) from signal count + indicator strength
            self.confidence = min(100, signals * 20)
            if abs(trend_strength) > 30:
                self.confidence = min(100, self.confidence + 10)
            if abs(ema_diff) > 0.5:
                self.confidence = min(100, self.confidence + 10)

            # Need at least 2 signals for confluence (lowered from 3)
            if signals < 2:
                return actions

            # Guard: max entry price
            if favored_price > self.MAX_ENTRY_PRICE:
                return actions

            # Guard: phase 3 needs high confidence
            if self.phase == 3 and self.confidence < 75:
                return actions

            # Guard: late cycle only at deep discount
            if time_remaining < 900 and favored_price > self.LATE_CYCLE_MAX_PRICE:
                return actions

            # Position sizing based on confidence
            if self.confidence >= 80:
                max_dollars = 20.0
            elif self.confidence >= 60:
                max_dollars = 15.0
            else:
                max_dollars = 10.0

            # Last 15 min: cap spend at $5 regardless of confidence (protect capital late cycle)
            if time_remaining < 900:
                max_dollars = min(max_dollars, 5.0)

            price_per_share = favored_price / 100.0
            shares = round(max_dollars / price_per_share, 1)

            # Check portfolio can afford it
            cost = shares * price_per_share
            if portfolio.balance < cost:
                shares = round(portfolio.balance / price_per_share, 1)
                if shares <= 0:
                    return actions

            # Execute entry
            actions.append({
                "action": "limit_buy_fill",
                "side": favored_side,
                "shares": shares,
                "price": favored_price
            })

            # Update internal state
            total_cost = (self.held_positions[favored_side] * self.held_avg_cost[favored_side]) + (shares * favored_price)
            self.held_positions[favored_side] += shares
            self.held_avg_cost[favored_side] = total_cost / self.held_positions[favored_side]
            self.last_entry_time = now
            self.position_entry_time = now
            self.peak_unrealized = 0.0
            self.profit_ever_hit_target = False

        return actions
