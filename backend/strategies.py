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
    ZONE1_MIN = 0.90              # $0.90 — immediate sell threshold
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

    def reset_state(self, full_reset: bool = False):
        """Reset after a trade exit. Keeps cycle_start_time so 3-min observation
        only runs once per cycle. Use full_reset=True on new cycle or redeploy."""
        self.held_positions = {'up': 0.0, 'down': 0.0}
        self.held_avg_cost = {'up': 0.0, 'down': 0.0}
        self.last_entry_time = 0.0
        self.position_entry_time = 0.0
        self.peak_unrealized = 0.0
        self.profit_ever_hit_target = False
        self.close_only_mode = False
        self.exit_reason = ""
        if full_reset:
            self.ema_fast = None
            self.ema_slow = None
            self.rsi_prices = []
            self.tick_count = 0
            self.cycle_drawdown = 0.0
            self.confidence = 0
            self.phase = 1
            self.cycle_start_time = 0.0  # Resets observation timer

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

            # --- ZONE 1: Time-adjusted instant sell ---
            # Last 5 min: $0.50+  |  Last 10 min: $0.70+  |  Last 30 min: $0.90+  |  First 30 min: $1.00+
            if time_remaining <= 300:
                zone1_min = 0.50
            elif time_remaining <= 600:
                zone1_min = 0.70
            elif time_remaining <= 1800:
                zone1_min = 0.90
            else:
                zone1_min = 1.00  # first 30 min: standard $1.00

            if zone1_min <= unrealized <= self.ZONE1_MAX:
                should_exit = True
                exit_reason = f"zone1_instant_sell (${unrealized:.2f}, {int(time_remaining//60)}min left)"

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

            # Position sizing based on confidence — keep trades small relative to $4 cycle cap
            if self.confidence >= 80:
                max_dollars = 10.0
            elif self.confidence >= 60:
                max_dollars = 7.0
            else:
                max_dollars = 5.0

            # Last 15 min: scale max spend by how extreme the odds are
            # 95/5 or 90/10 (underdog ≤10c) → max $1
            # 80/20 or 60/40 (underdog 11–40c) → max $3
            # 50/50 area or balanced → max $5
            if time_remaining < 900:
                cheaper_side = min(up_price, down_price)
                if cheaper_side <= 10:
                    late_cap = 1.0
                elif cheaper_side <= 40:
                    late_cap = 3.0
                else:
                    late_cap = 5.0
                max_dollars = min(max_dollars, late_cap)

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


# ============================================================================
#  NEW CLAUDE ALL-IN-ONE STRATEGY
#  Unified BTC-price + technical strategy. Multi-trade scalping with position
#  flip. Target $4/hr through 4-8 small trades.
# ============================================================================

class NCAIOConfig:
    """User-adjustable config sent from frontend."""
    def __init__(self):
        self.trade_size_preset: str = "medium"   # "small"=$5, "medium"=$8, "large"=$12
        self.risk_level: str = "balanced"         # "conservative", "balanced", "aggressive"
        self.position_flip_enabled: bool = True
        self.profit_mode: str = "smart"           # "quick"=$0.50, "patient"=$1.00+, "smart"=time-adjusted
        self.max_trades_per_cycle: int = 8
        self.stop_loss_per_trade: float = 1.50    # was 2.00 — tighter to reduce loss per trade
        # AI confirmation
        self.ai_confirm_enabled: bool = False
        self.ai_model: str = "gemini-flash"
        self.ai_api_key: str = ""

    def get_trade_dollars(self) -> float:
        return {"small": 5.0, "medium": 8.0, "large": 12.0}.get(self.trade_size_preset, 8.0)

    def get_min_signals(self) -> int:
        return {"conservative": 3, "balanced": 2, "aggressive": 1}.get(self.risk_level, 2)

    def get_max_entry_price(self) -> int:
        return {"conservative": 50, "balanced": 55, "aggressive": 60}.get(self.risk_level, 55)

    def get_min_confidence(self) -> int:
        return {"conservative": 45, "balanced": 30, "aggressive": 15}.get(self.risk_level, 30)


class NewClaudeAllInOneStrategy:
    """
    New Claude All-in-One: Unified BTC-price + Technical Strategy

    Core design:
    - BTC price vs target as PRIMARY signal (40% weight)
    - EMA crossover + RSI + velocity + entry zone as secondary signals
    - Multiple small trades per cycle ($5-12 each, target $0.50-$1.00 profit)
    - Position flip on stop-loss when velocity confirms reversal
    - Internal 20-min price history buffer
    - Spread-aware execution
    - Market-condition-based phases (not just time-based)
    """

    EMA_FAST_ALPHA = 2.0 / 16.0   # 15-tick EMA
    EMA_SLOW_ALPHA = 2.0 / 46.0   # 45-tick EMA
    RSI_PERIOD = 14
    ANTI_WHIPSAW_SECONDS = 20.0
    FLIP_COOLDOWN_SECONDS = 60.0
    TIMEOUT_MINUTES = 8
    OBSERVE_SECONDS = 120
    HISTORY_WINDOW = 1200
    MAX_DRAWDOWN = -6.0
    CONSECUTIVE_LOSS_CAUTIOUS = 2
    STOPLOSS_GRACE_SECONDS = 180  # 3-min grace period — no stop-loss on fresh trades

    def __init__(self):
        self.config = NCAIOConfig()
        self.trades: list = []
        self.trade_counter = 0
        self.completed_trade_count = 0
        self.consecutive_losses = 0
        self.held_positions = {'up': 0.0, 'down': 0.0}
        self.held_avg_cost = {'up': 0.0, 'down': 0.0}
        self.price_history: list = []
        self.ema_fast = None
        self.ema_slow = None
        self.rsi_prices: list = []
        self.tick_count = 0
        self.phase = "observing"
        self.confidence = 0
        self.cycle_start_time = 0.0
        self.last_entry_time = 0.0
        self.last_flip_time = 0.0
        self.exit_reason = ""
        self.cycle_realized_pnl = 0.0
        self.estimated_spread = 3.0

    def reset_state(self, full_reset: bool = False):
        self.trades = []
        self.trade_counter = 0
        self.completed_trade_count = 0
        self.consecutive_losses = 0
        self.held_positions = {'up': 0.0, 'down': 0.0}
        self.held_avg_cost = {'up': 0.0, 'down': 0.0}
        self.last_entry_time = 0.0
        self.last_flip_time = 0.0
        self.exit_reason = ""
        self.cycle_realized_pnl = 0.0
        if full_reset:
            self.ema_fast = None
            self.ema_slow = None
            self.rsi_prices = []
            self.tick_count = 0
            self.confidence = 0
            self.phase = "observing"
            self.cycle_start_time = 0.0
            self.price_history = []

    def _update_ema(self, price: float):
        if self.ema_fast is None:
            self.ema_fast = price
            self.ema_slow = price
        else:
            self.ema_fast += self.EMA_FAST_ALPHA * (price - self.ema_fast)
            self.ema_slow += self.EMA_SLOW_ALPHA * (price - self.ema_slow)

    def _compute_rsi(self) -> float:
        if len(self.rsi_prices) < self.RSI_PERIOD + 1:
            return 50.0
        changes = [self.rsi_prices[i] - self.rsi_prices[i - 1]
                    for i in range(len(self.rsi_prices) - self.RSI_PERIOD, len(self.rsi_prices))]
        gains = [c for c in changes if c > 0]
        losses = [-c for c in changes if c < 0]
        avg_gain = sum(gains) / self.RSI_PERIOD if gains else 0.001
        avg_loss = sum(losses) / self.RSI_PERIOD if losses else 0.001
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _compute_velocity(self, window_sec: float) -> float:
        now = time.time()
        cutoff = now - window_sec
        recent = [(t, p) for t, p, _, _ in self.price_history if t >= cutoff]
        if len(recent) < 2:
            return 0.0
        dt = recent[-1][0] - recent[0][0]
        if dt < 1.0:
            return 0.0
        return (recent[-1][1] - recent[0][1]) / dt

    def _is_choppy_market(self) -> bool:
        """Detect choppy/oscillating market — 6+ direction reversals in 2 min with <2c net move."""
        now = time.time()
        cutoff = now - 120
        recent = [(t, p) for t, p, _, _ in self.price_history if t >= cutoff]
        if len(recent) < 5:
            return False
        reversals = 0
        for i in range(2, len(recent)):
            d1 = recent[i - 1][1] - recent[i - 2][1]
            d2 = recent[i][1] - recent[i - 1][1]
            if d1 * d2 < 0:
                reversals += 1
        net_move = abs(recent[-1][1] - recent[0][1])
        return reversals >= 6 and net_move < 2.0

    def _min_movement_ok(self) -> bool:
        """Require >= 2c price range in last 2 min to enter."""
        now = time.time()
        cutoff = now - 120
        recent = [p for t, p, _, _ in self.price_history if t >= cutoff]
        if len(recent) < 3:
            return False
        return (max(recent) - min(recent)) >= 2.0

    def should_ai_confirm(self, confidence: int) -> bool:
        """Returns True if AI confirmation should be sought before entry."""
        if self.config.ai_confirm_enabled and self.config.ai_api_key:
            return True
        # Force AI confirm in recovery mode if key is available
        if self.config.ai_api_key and self.cycle_realized_pnl < -1.0:
            return True
        return False

    def confirm_pending_entry(self, action: dict, now: float, time_remaining: float):
        """Called by main.py after AI confirms the trade."""
        self._open_trade(action["side"], action["shares"], action["price"], now, time_remaining)

    def cancel_pending_entry(self):
        """Called by main.py if AI rejects the trade."""
        pass  # Nothing to undo since trade was never opened

    def _compute_composite_signal(self, up_price: int, down_price: int,
                                   btc_price: float, price_to_beat: float,
                                   time_remaining: float) -> tuple:
        total_score = 0.0
        reasons = []

        # Signal 1: BTC price vs target (weight: 40)
        if btc_price > 0 and price_to_beat > 0:
            delta = btc_price - price_to_beat
            time_factor = min(1.0, time_remaining / 3600.0) * 1.5
            btc_signal = max(-40.0, min(40.0, (delta / 10.0) * time_factor))
            if abs(btc_signal) > 5:
                reasons.append(f"BTC ${abs(delta):.0f} {'above' if delta > 0 else 'below'}")
            total_score += btc_signal

        # Signal 2: EMA crossover (weight: 20)
        if self.ema_fast is not None and self.ema_slow is not None:
            ema_diff = self.ema_fast - self.ema_slow
            if abs(ema_diff) > 0.3:
                ema_signal = max(-20.0, min(20.0, ema_diff * 20.0))
                reasons.append(f"EMA {'bull' if ema_diff > 0 else 'bear'}")
                total_score += ema_signal

        # Signal 3: Price velocity (weight: 15)
        vel_2m = self._compute_velocity(120)
        vel_5m = self._compute_velocity(300)
        if abs(vel_2m) > 0.1:
            vel_signal = max(-15.0, min(15.0, vel_2m * 50.0))
            if vel_5m * vel_2m > 0:
                vel_signal *= 1.2
            else:
                vel_signal *= 0.6
            vel_signal = max(-15.0, min(15.0, vel_signal))
            reasons.append(f"vel {vel_2m:+.2f}c/s")
            total_score += vel_signal

        # Signal 4: RSI (weight: 10)
        rsi = self._compute_rsi()
        if rsi > 70:
            total_score += -min(10.0, (rsi - 70) * 0.33)
            reasons.append(f"RSI {rsi:.0f}")
        elif rsi < 30:
            total_score += min(10.0, (30 - rsi) * 0.33)
            reasons.append(f"RSI {rsi:.0f}")
        else:
            total_score += (rsi - 50) * 0.1

        # Signal 5: Entry zone (weight: 15)
        entry_price = up_price if total_score > 0 else down_price if total_score < 0 else min(up_price, down_price)
        if 30 <= entry_price <= 50:
            zone_signal = 15.0
        elif 50 < entry_price <= 60:
            zone_signal = 8.0
        elif entry_price > 60:
            zone_signal = -5.0
        else:
            zone_signal = 3.0
        if total_score < 0:
            zone_signal = -zone_signal
        total_score += zone_signal

        side = "up" if total_score > 0 else "down" if total_score < 0 else "none"
        confidence = min(100, int(abs(total_score)))
        reason_str = " | ".join(reasons[:3]) if reasons else "weak signals"
        return (side, confidence, reason_str)

    def _determine_phase(self, time_remaining: float) -> str:
        now = time.time()
        if self.cycle_start_time > 0 and (now - self.cycle_start_time) < self.OBSERVE_SECONDS:
            return "observing"
        if time_remaining < 300:
            return "exit_only"
        if self.consecutive_losses >= self.CONSECUTIVE_LOSS_CAUTIOUS or time_remaining < 600:
            return "cautious"
        if self.cycle_realized_pnl <= self.MAX_DRAWDOWN:
            return "exit_only"
        return "active"

    def _get_active_trade(self):
        for t in self.trades:
            if t["status"] == "active":
                return t
        return None

    def _has_active_trade(self) -> bool:
        return self._get_active_trade() is not None

    def on_tick(self, time_remaining: float, up_price: int, down_price: int,
                portfolio, history: list, cycle_profit: float = 0.0,
                profit_target: float = 4.0,
                btc_price: float = 0.0, price_to_beat: float = 0.0,
                spread_cents: float = 3.0) -> list:
        now = time.time()
        actions = []

        if self.cycle_start_time == 0.0:
            self.cycle_start_time = now

        # Update internal history (20-min buffer)
        self.price_history.append((now, float(up_price), float(down_price), btc_price))
        cutoff = now - self.HISTORY_WINDOW
        self.price_history = [h for h in self.price_history if h[0] >= cutoff]

        self.tick_count += 1
        self._update_ema(float(up_price))
        self.rsi_prices.append(float(up_price))
        if len(self.rsi_prices) > 100:
            self.rsi_prices = self.rsi_prices[-100:]

        if spread_cents > 0:
            self.estimated_spread = spread_cents

        self.phase = self._determine_phase(time_remaining)

        side, confidence, reason = self._compute_composite_signal(
            up_price, down_price, btc_price, price_to_beat, time_remaining
        )
        self.confidence = confidence

        # EXITS first
        active_trade = self._get_active_trade()
        if active_trade:
            actions.extend(self._evaluate_exit(active_trade, up_price, down_price,
                                                time_remaining, now, side, confidence))

        # ENTRIES only if no active trade
        if not self._has_active_trade() and self.phase in ("active", "cautious"):
            actions.extend(self._evaluate_entry(side, confidence, reason,
                                                 up_price, down_price,
                                                 time_remaining, now, portfolio,
                                                 cycle_profit, profit_target))
        return actions

    def _evaluate_exit(self, trade: dict, up_price: int, down_price: int,
                       time_remaining: float, now: float,
                       current_signal_side: str, current_confidence: int) -> list:
        actions = []
        side = trade["side"]
        shares = trade["shares"]
        entry_price = trade["entry_price"]
        current_price = up_price if side == "up" else down_price
        unrealized = shares * (current_price - entry_price) / 100.0
        hold_seconds = now - trade["entry_time"]

        if unrealized > trade.get("peak_pnl", 0.0):
            trade["peak_pnl"] = unrealized

        # 1. TIME STOP — last 2 min
        if time_remaining < 120:
            self._close_trade(trade, current_price, unrealized, "time-stop 2min")
            actions.append({"action": "market_sell", "side": side, "shares": shares,
                           "price": current_price, "trade_pnl": unrealized})
            return actions

        # 2. PHASE EXIT
        if self.phase == "exit_only":
            self._close_trade(trade, current_price, unrealized, "phase exit")
            actions.append({"action": "market_sell", "side": side, "shares": shares,
                           "price": current_price, "trade_pnl": unrealized})
            return actions

        # 3. STOP-LOSS + optional position flip (with 3-min grace period)
        if hold_seconds > self.STOPLOSS_GRACE_SECONDS and unrealized <= -self.config.stop_loss_per_trade:
            self._close_trade(trade, current_price, unrealized, f"stop-loss ${unrealized:.2f}")
            actions.append({"action": "market_sell", "side": side, "shares": shares,
                           "price": current_price, "trade_pnl": unrealized})
            # Position flip
            if (self.config.position_flip_enabled
                    and time_remaining > 600
                    and (now - self.last_flip_time) > self.FLIP_COOLDOWN_SECONDS
                    and self.completed_trade_count < self.config.max_trades_per_cycle):
                vel_2m = self._compute_velocity(120)
                opposite = "down" if side == "up" else "up"
                flip_ok = (side == "up" and vel_2m < -0.15) or (side == "down" and vel_2m > 0.15)
                if flip_ok:
                    flip_price = down_price if opposite == "down" else up_price
                    if flip_price <= self.config.get_max_entry_price():
                        dollars = min(self.config.get_trade_dollars(), 5.0)
                        flip_shares = dollars / (flip_price / 100.0)
                        self._open_trade(opposite, flip_shares, flip_price, now, time_remaining)
                        self.last_flip_time = now
                        actions.append({"action": "limit_buy_fill", "side": opposite,
                                       "shares": flip_shares, "price": flip_price})
                        print(f"[NCAIO] FLIP {side}->{opposite} @ {flip_price}c vel={vel_2m:+.2f}")
            return actions

        # 4. QUICK PROFIT
        if self.config.profit_mode == "quick" and unrealized >= 0.50:
            self._close_trade(trade, current_price, unrealized, f"quick +${unrealized:.2f}")
            actions.append({"action": "market_sell", "side": side, "shares": shares,
                           "price": current_price, "trade_pnl": unrealized})
            return actions

        # 5. PATIENT PROFIT
        if self.config.profit_mode == "patient":
            target = 1.00 if time_remaining > 1800 else 0.80 if time_remaining > 600 else 0.60
            if target <= unrealized <= 1.50:
                self._close_trade(trade, current_price, unrealized, f"patient +${unrealized:.2f}")
                actions.append({"action": "market_sell", "side": side, "shares": shares,
                               "price": current_price, "trade_pnl": unrealized})
                return actions
            peak = trade.get("peak_pnl", 0.0)
            if peak > 1.50 and unrealized < (peak - 0.15):
                self._close_trade(trade, current_price, unrealized, f"trail +${unrealized:.2f}")
                actions.append({"action": "market_sell", "side": side, "shares": shares,
                               "price": current_price, "trade_pnl": unrealized})
                return actions

        # 5b. SMART PROFIT (time-adjusted from Claude Strategy — best of both modes)
        if self.config.profit_mode == "smart":
            if time_remaining <= 300:
                target = 0.50
            elif time_remaining <= 600:
                target = 0.60
            elif time_remaining <= 1800:
                target = 0.80
            else:
                target = 1.00
            # Instant sell zone: take profit when in target range
            if target <= unrealized <= 1.50:
                self._close_trade(trade, current_price, unrealized, f"smart +${unrealized:.2f} (target ${target:.2f})")
                actions.append({"action": "market_sell", "side": side, "shares": shares,
                               "price": current_price, "trade_pnl": unrealized})
                return actions
            # Trailing stop above $1.50 — let winners run
            peak = trade.get("peak_pnl", 0.0)
            if peak > 1.50 and unrealized < (peak - 0.15):
                self._close_trade(trade, current_price, unrealized, f"smart-trail +${unrealized:.2f} (peak ${peak:.2f})")
                actions.append({"action": "market_sell", "side": side, "shares": shares,
                               "price": current_price, "trade_pnl": unrealized})
                return actions

        # 6. TIMEOUT
        if hold_seconds > self.TIMEOUT_MINUTES * 60 and 0.10 <= unrealized < 0.80:
            self._close_trade(trade, current_price, unrealized, f"timeout +${unrealized:.2f}")
            actions.append({"action": "market_sell", "side": side, "shares": shares,
                           "price": current_price, "trade_pnl": unrealized})
            return actions

        return actions

    def _close_trade(self, trade: dict, exit_price: int, pnl: float, reason: str):
        trade["status"] = "closed"
        trade["exit_price"] = exit_price
        trade["pnl"] = pnl
        self.exit_reason = reason
        self.completed_trade_count += 1
        self.cycle_realized_pnl += pnl
        side = trade["side"]
        self.held_positions[side] = max(0, self.held_positions[side] - trade["shares"])
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        print(f"[NCAIO] EXIT {side.upper()} #{trade['id']} | {reason} | PnL: ${pnl:+.2f} | Cycle: ${self.cycle_realized_pnl:+.2f}")

    def _open_trade(self, side: str, shares: float, price: int, now: float, time_remaining: float):
        self.trade_counter += 1
        trade = {
            "id": self.trade_counter, "side": side, "shares": shares,
            "entry_price": price, "entry_time": now, "entry_time_left": time_remaining,
            "peak_pnl": 0.0, "status": "active",
        }
        self.trades.append(trade)
        self.held_positions[side] += shares
        self.last_entry_time = now
        print(f"[NCAIO] ENTRY {side.upper()} #{trade['id']} | {shares:.1f}sh @ {price}c | ${shares * price / 100:.2f}")

    def _evaluate_entry(self, side: str, confidence: int, reason: str,
                        up_price: int, down_price: int,
                        time_remaining: float, now: float, portfolio,
                        cycle_profit: float, profit_target: float) -> list:
        actions = []
        if side == "none":
            return actions
        if self.completed_trade_count >= self.config.max_trades_per_cycle:
            return actions
        if cycle_profit >= profit_target:
            return actions
        if self.cycle_realized_pnl <= self.MAX_DRAWDOWN:
            return actions
        if time_remaining < 300:
            return actions
        if (now - self.last_entry_time) < self.ANTI_WHIPSAW_SECONDS:
            return actions

        min_conf = self.config.get_min_confidence()
        if self.phase == "cautious":
            min_conf = max(min_conf, 75)

        # Recovery mode: raise confidence bar after losses
        if self.cycle_realized_pnl < 0:
            min_conf = max(min_conf, 60)
        if self.consecutive_losses >= 2:
            min_conf = max(min_conf, 75)

        if confidence < min_conf:
            return actions

        entry_price = up_price if side == "up" else down_price
        if entry_price > self.config.get_max_entry_price():
            return actions

        # Choppy market filter — skip noisy oscillations
        if self._is_choppy_market():
            return actions

        # Min movement filter — need >= 2c range to have edge
        if not self._min_movement_ok():
            return actions

        # Momentum-against filter — don't fight strong opposite momentum
        vel_2m = self._compute_velocity(120)
        if side == "up" and vel_2m < -0.25:
            return actions
        if side == "down" and vel_2m > 0.25:
            return actions

        # Spread check
        spread = self.estimated_spread
        effective_cost = entry_price + spread / 2
        min_target_cents = 5.0 if self.config.profit_mode == "quick" else 10.0
        if (100 - effective_cost) < (min_target_cents + spread):
            return actions

        # Pullback detection
        vel_30s = self._compute_velocity(30)
        vel_10s = self._compute_velocity(10)
        if abs(vel_30s) > 0.3:
            if side == "up" and vel_30s > 0.3 and vel_10s > 0:
                return actions
            if side == "down" and vel_30s < -0.3 and vel_10s < 0:
                return actions

        # Sizing
        dollars = self.config.get_trade_dollars()
        if time_remaining < 900:
            dollars = min(dollars, 5.0)

        # Recovery sizing: if behind target and high confidence, increase slightly
        deficit = profit_target - cycle_profit - self.cycle_realized_pnl
        if deficit > 2.0 and confidence >= 75:
            dollars = min(dollars * 1.25, 15.0)

        shares = dollars / (entry_price / 100.0)
        cost = shares * (entry_price / 100.0)
        if portfolio.balance < cost:
            shares = portfolio.balance / (entry_price / 100.0)
            cost = shares * (entry_price / 100.0)
        if shares <= 0 or cost < 0.10:
            return actions

        # AI confirmation: return pending_buy instead of opening trade
        if self.should_ai_confirm(confidence):
            actions.append({
                "action": "pending_buy", "side": side, "shares": shares,
                "price": entry_price, "confidence": confidence, "reason": reason,
            })
            return actions

        self._open_trade(side, shares, entry_price, now, time_remaining)
        actions.append({"action": "limit_buy_fill", "side": side, "shares": shares, "price": entry_price})
        return actions
