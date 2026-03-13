def calculate_sequence():
    trades = []
    
    # Sequence of prices we encounter. 
    # Tuple: (Direction we are buying, Price of that direction in cents)
    # E.g., market is bouncing back and forth. 
    # 1. We buy UP at 45c
    # 2. Market drops, we buy DOWN at 50c
    # 3. Market rises, we buy UP at 55c
    # 4. Market drops, we buy DOWN at 60c
    
    scenarios = [
        ('UP', 0.45),
        ('DOWN', 0.50),
        ('UP', 0.55),
        ('DOWN', 0.60),
        ('UP', 0.65),
        ('DOWN', 0.70),
        ('UP', 0.75),
        ('DOWN', 0.80),
    ]
    
    total_spent = 0.0
    positions = {'UP': 0.0, 'DOWN': 0.0}
    target_profit = 1.0
    
    print("=== Polymarket Martingale Balancing Calculator ===")
    print(f"Goal: Always try to ensure the side we just bought will yield a ${target_profit} net profit.\n")
    
    for i, (direction, price) in enumerate(scenarios, 1):
        # We want: (positions[direction] + new_shares) * 1.0 = total_spent + new_cost + target_profit
        # new_cost = new_shares * price
        # positions[direction] + new_shares = total_spent + new_shares * price + target_profit
        # new_shares * (1 - price) = total_spent + target_profit - positions[direction]
        
        required_payout = total_spent + target_profit - positions[direction]
        
        if required_payout <= 0:
            print(f"Trade {i}: No need to buy {direction}, existing position already covers target profit!")
            continue
            
        new_shares = required_payout / (1 - price)
        new_cost = new_shares * price
        
        # update state
        total_spent += new_cost
        positions[direction] += new_shares
        
        print(f"--- Trade {i} ---")
        print(f"Market moved against us. Now buying {direction} at {int(price*100)}¢")
        print(f"  Existing {direction} shares : {positions[direction]-new_shares:.2f}")
        print(f"  Total Sunk Cost so far      : ${total_spent-new_cost:.2f}")
        print(f"  We need to buy              : {new_shares:.2f} shares")
        print(f"  Cost of this trade          : ${new_cost:.2f}")
        print(f"  New Total Spent             : ${total_spent:.2f}")
        
        # Verify payouts
        payout_if_up = positions['UP']
        payout_if_down = positions['DOWN']
        
        profit_if_up = payout_if_up - total_spent
        profit_if_down = payout_if_down - total_spent
        
        print(f"  >>> If UP wins   -> Gross Payout: ${payout_if_up:.2f} | Net Profit: ${profit_if_up:.2f}")
        print(f"  >>> If DOWN wins -> Gross Payout: ${payout_if_down:.2f} | Net Profit: ${profit_if_down:.2f}\n")

if __name__ == '__main__':
    calculate_sequence()
