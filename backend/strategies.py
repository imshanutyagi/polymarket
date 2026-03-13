from typing import Dict, Optional, Any

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
