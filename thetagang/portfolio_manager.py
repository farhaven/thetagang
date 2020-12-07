import math

import click
import ib_insync
from ib_insync import util
from ib_insync.contract import ComboLeg, Contract, Option, Stock, TagValue
from ib_insync.order import LimitOrder, Order

from thetagang.util import count_option_positions, position_pnl

from .options import option_dte


class PortfolioManager:
    def __init__(self, config, ib, completion_future):
        self.config = config
        self.ib = ib
        self.completion_future = completion_future

    def get_calls(self, portfolio_positions):
        return self.get_options(portfolio_positions, "C")

    def get_puts(self, portfolio_positions):
        return self.get_options(portfolio_positions, "P")

    def get_options(self, portfolio_positions, right):
        r = []
        for symbol in portfolio_positions:
            r = r + list(
                filter(
                    lambda p: (
                        isinstance(p.contract, Option)
                        and p.contract.right.startswith(right)
                    ),
                    portfolio_positions[symbol],
                )
            )

        return r

    def put_is_itm(self, contract):
        stock = Stock(contract.symbol, "SMART", currency="USD")
        [ticker] = self.ib.reqTickers(stock)
        return contract.strike >= ticker.marketPrice()

    def put_can_be_rolled(self, put):
        # Check if this put is ITM. Do not roll ITM puts.
        if self.put_is_itm(put.contract):
            return False

        dte = option_dte(put.contract.lastTradeDateOrContractMonth)
        pnl = position_pnl(put)

        if dte <= self.config["roll_when"]["dte"]:
            click.secho(
                f"{put.contract.localSymbol} can be rolled because DTE of {dte} is <= {self.config['roll_when']['dte']}",
                fg="blue",
            )
            return True

        if pnl >= self.config["roll_when"]["pnl"]:
            click.secho(
                f"{put.contract.localSymbol} can be rolled because P&L of {round(pnl * 100, 1)}% is >= {round(self.config['roll_when']['pnl'] * 100,1)}",
                fg="blue",
            )
            return True

        return False

    def call_can_be_rolled(self, call):
        dte = option_dte(call.contract.lastTradeDateOrContractMonth)
        pnl = position_pnl(call)

        if dte <= self.config["roll_when"]["dte"]:
            click.secho(
                f"{call.contract.localSymbol} can be rolled because DTE of {dte} is <= {self.config['roll_when']['dte']}",
                fg="blue",
            )
            return True

        if pnl >= self.config["roll_when"]["pnl"]:
            click.secho(
                f"{call.contract.localSymbol} can be rolled because P&L of {round(pnl * 100, 1)}% is >= {round(self.config['roll_when']['pnl'] * 100,1)}",
                fg="blue",
            )
            return True

        return False

    def filter_positions(self, portfolio_positions):
        keys = portfolio_positions.keys()
        for k in keys:
            if k not in self.config["symbols"]:
                del portfolio_positions[k]
        return portfolio_positions

    def manage(self, account_summary, portfolio_positions):
        click.echo()
        click.secho("Checking positions...", fg="green")
        click.echo()

        portfolio_positions = self.filter_positions(portfolio_positions)

        self.check_puts(portfolio_positions)
        self.check_calls(portfolio_positions)

        # Look for lots of stock that don't have covered calls
        self.check_for_uncovered_positions(portfolio_positions)

        # Refresh positions
        portfolio_positions = self.ib.portfolio()

        # Check if we have enough buying power to write some puts
        self.check_if_can_write_puts(account_summary, portfolio_positions)

        # Shut it down
        self.completion_future.set_result(True)

    def check_puts(self, portfolio_positions):
        # Check for puts which may be rolled to the next expiration or a better price
        puts = self.get_puts(portfolio_positions)

        # find puts eligible to be rolled
        rollable_puts = list(filter(lambda p: self.put_can_be_rolled(p), puts))

        click.secho(f"{len(rollable_puts)} puts will be rolled", fg="green")

        self.roll_puts(rollable_puts)

    def check_calls(self, portfolio_positions):
        # Check for calls which may be rolled to the next expiration or a better price
        calls = self.get_calls(portfolio_positions)

        # find calls eligible to be rolled
        rollable_calls = list(filter(lambda p: self.call_can_be_rolled(p), calls))

        click.secho(f"{len(rollable_calls)} calls will be rolled", fg="green")

        self.roll_calls(rollable_calls)

    def check_for_uncovered_positions(self, portfolio_positions):
        for symbol in portfolio_positions:
            call_count = count_option_positions(symbol, portfolio_positions, "C")
            stock_count = math.floor(
                sum(
                    [
                        p.position
                        for p in portfolio_positions[symbol]
                        if isinstance(p.contract, Stock)
                    ]
                )
            )

            target_calls = stock_count // 100

            calls_to_write = target_calls - call_count

            if calls_to_write > 0:
                click.secho(f"Need to write {calls_to_write} for {symbol}", fg="green")
                self.write_calls(symbol, calls_to_write)

    def write_calls(self, symbol, quantity):
        sell_ticker = self.find_eligible_contracts(symbol, "C")

        # Create order
        order = LimitOrder(
            "SELL",
            quantity,
            round(sell_ticker.marketPrice(), 2),
            algoStrategy="Adaptive",
            algoParams=[TagValue("adaptivePriority", "Patient")],
            tif="DAY",
        )

        # Submit order
        trade = self.ib.placeOrder(sell_ticker.contract, order)
        click.secho("Order submitted", fg="green")
        click.secho(f"{trade}", fg="green")

    def write_puts(self, symbol, quantity):
        sell_ticker = self.find_eligible_contracts(symbol, "P")

        # Create order
        order = LimitOrder(
            "SELL",
            quantity,
            round(sell_ticker.marketPrice(), 2),
            algoStrategy="Adaptive",
            algoParams=[TagValue("adaptivePriority", "Patient")],
            tif="DAY",
        )

        # Submit order
        trade = self.ib.placeOrder(sell_ticker.contract, order)
        click.secho("Order submitted", fg="green")
        click.secho(f"{trade}", fg="green")

    def check_if_can_write_puts(self, account_summary, portfolio_positions):
        # Get stock positions
        stocks = [
            position
            for symbol in portfolio_positions
            for position in portfolio_positions[symbol]
            if isinstance(position.contract, Stock)
        ]

        remaining_buying_power = math.floor(
            min(
                [
                    float(account_summary["BuyingPower"].value),
                    float(account_summary["ExcessLiquidity"].value)
                    - float(account_summary["NetLiquidation"].value)
                    * self.config["account"]["minimum_cushion"],
                ]
            )
        )

        # Sum stock values
        total_value = (
            sum([stock.marketValue * stock.position for stock in stocks])
            + remaining_buying_power
        )

        stock_symbols = dict()
        for stock in stocks:
            symbol = stock.contract.symbol
            stock_symbols[symbol] = stock

        targets = dict()
        target_additional_quantity = dict()

        # Determine target quantity of each stock
        for symbol in self.config["symbols"].keys():
            stock = Stock(symbol, "SMART", currency="USD")
            [ticker] = self.ib.reqTickers(stock)

            targets[symbol] = self.config["symbols"][symbol]["weight"] * total_value
            target_additional_quantity[symbol] = math.floor(
                targets[symbol] / ticker.marketPrice()
            )

            if symbol in stock_symbols:
                target_additional_quantity[symbol] = (
                    target_additional_quantity[symbol] - stock_symbols[symbol].position
                )

        # Figure out how many addition puts are needed, if they're needed
        for symbol in target_additional_quantity.keys():
            put_count = count_option_positions(symbol, portfolio_positions, "P")
            target_put_count = target_additional_quantity[symbol] // 100
            if put_count < target_put_count:
                self.write_puts(symbol, target_put_count - put_count)

        return

    def roll_puts(self, puts):
        return self.roll_positions(puts, "P")

    def roll_calls(self, calls):
        return self.roll_positions(calls, "C")

    def roll_positions(self, positions, right):
        for position in positions:
            symbol = position.contract.symbol
            sell_ticker = self.find_eligible_contracts(symbol, right)
            quantity = abs(position.position)

            position.contract.exchange = "SMART"
            [buy_ticker] = self.ib.reqTickers(position.contract)

            price = buy_ticker.marketPrice() - sell_ticker.marketPrice()

            # Create combo legs
            comboLegs = [
                ComboLeg(
                    conId=position.contract.conId,
                    ratio=1,
                    exchange="SMART",
                    action="BUY",
                ),
                ComboLeg(
                    conId=sell_ticker.contract.conId,
                    ratio=1,
                    exchange="SMART",
                    action="SELL",
                ),
            ]

            # Create contract
            combo = Contract(
                secType="BAG",
                symbol=symbol,
                currency="USD",
                exchange="SMART",
                comboLegs=comboLegs,
            )

            # Create order
            order = LimitOrder(
                "BUY",
                quantity,
                round(price, 2),
                algoStrategy="Adaptive",
                algoParams=[TagValue("adaptivePriority", "Patient")],
                tif="DAY",
            )

            # Submit order
            trade = self.ib.placeOrder(combo, order)
            click.secho("Order submitted", fg="green")
            click.secho(f"{trade}", fg="green")

    def find_eligible_contracts(self, symbol, right):
        stock = Stock(symbol, "SMART", currency="USD")
        contracts = self.ib.qualifyContracts(stock)

        [ticker] = self.ib.reqTickers(stock)
        tickerValue = ticker.marketPrice()

        chains = self.ib.reqSecDefOptParams(
            stock.symbol, "", stock.secType, stock.conId
        )
        chain = next(c for c in chains if c.exchange == "SMART")

        def valid_strike(strike):
            if right.startswith("P"):
                return strike <= tickerValue
            if right.startswith("C"):
                return strike >= tickerValue
            return False

        chain_expirations = self.config["option_chains"]["expirations"]

        strikes = sorted(strike for strike in chain.strikes if valid_strike(strike))
        expirations = sorted(
            exp
            for exp in chain.expirations
            if option_dte(exp) >= self.config["target"]["dte"]
        )[:chain_expirations]
        rights = [right]

        def nearest_strikes(strikes):
            chain_strikes = self.config["option_chains"]["strikes"]
            if right.startswith("P"):
                return strikes[-chain_strikes:]
            if right.startswith("C"):
                return strikes[:chain_strikes]

        contracts = [
            Option(
                symbol,
                expiration,
                strike,
                right,
                "SMART",
                tradingClass=chain.tradingClass,
            )
            for right in rights
            for expiration in expirations
            for strike in nearest_strikes(strikes)
        ]

        contracts = self.ib.qualifyContracts(*contracts)

        tickers = self.ib.reqTickers(*contracts)

        def open_interest_is_valid(ticker):
            ticker = self.ib.reqMktData(ticker.contract, genericTickList="101")

            while util.isNan(ticker.putOpenInterest) or util.isNan(
                ticker.callOpenInterest
            ):
                self.ib.waitOnUpdate(timeout=2)

            self.ib.cancelMktData(ticker.contract)

            # The open interest value is never present when using historical
            # data, so just ignore it when the value is None
            if right.startswith("P"):
                return (
                    ticker.putOpenInterest
                    >= self.config["target"]["minimum_open_interest"]
                )
            if right.startswith("C"):
                return (
                    ticker.callOpenInterest
                    >= self.config["target"]["minimum_open_interest"]
                )

        def delta_is_valid(ticker):
            return (
                ticker.modelGreeks
                and ticker.modelGreeks.delta
                and abs(ticker.modelGreeks.delta) <= self.config["target"]["delta"]
            )

        # Filter by delta and open interest
        tickers = [ticker for ticker in tickers if delta_is_valid(ticker)]
        tickers = [ticker for ticker in tickers if open_interest_is_valid(ticker)]
        tickers = sorted(
            reversed(sorted(tickers, key=lambda t: abs(t.modelGreeks.delta))),
            key=lambda t: option_dte(t.contract.lastTradeDateOrContractMonth),
        )

        if len(tickers) == 0:
            raise RuntimeError(f"No valid contracts found for {symbol}. Aborting.")

        return tickers[0]