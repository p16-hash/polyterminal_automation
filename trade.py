import time

class TradingStrategy:
    def __init__(self):
        self.limit_orders = []

    def place_limit_order(self, side, price, amount):
        order = {'side': side, 'price': price, 'amount': amount, 'timestamp': time.time()}
        self.limit_orders.append(order)
        return order

    def check_orders(self):
        current_time = time.time()
        for order in self.limit_orders[:]:  # Copy to avoid modification during iteration
            if current_time - order['timestamp'] > 180:
                # Cancel the order if it has been open for more than 3 minutes
                self.cancel_order(order)

    def cancel_order(self, order):
        print(f"Cancelling order: {order}")
        self.limit_orders.remove(order)

    def execute_trade(self):
        # Implement your trading execution logic here
        # Automatically sell and restart the round if only one side fills
        filled_orders = [order for order in self.limit_orders if self.is_filled(order)]
        if len(filled_orders) == 1:
            self.sell_and_restart(filled_orders[0])

    def is_filled(self, order):
        # Placeholder implementation for checking if an order is filled
        return False  # Modify based on your conditions

    def sell_and_restart(self, filled_order):
        print(f"Selling filled order: {filled_order}")
        self.limit_orders.clear()  # Clear unfilled orders
        # Restart the process for the next trading round

# Example usage of the TradingStrategy class
trading_strategy = TradingStrategy()

# Place some limit orders
trading_strategy.place_limit_order('buy', 100, 1)
trading_strategy.place_limit_order('sell', 105, 1)

# Check orders and execute trades accordingly
trading_strategy.check_orders()
trading_strategy.execute_trade()