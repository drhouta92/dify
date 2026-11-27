"""swingbot v4: Shariah-screened swing trading bot for the Alpaca PAPER environment.

Layering (imports only point downward):

    cli -> trader -> execution -> store, broker, risk
                  -> universe  -> shariah, liquidity
                  -> strategy  -> indicators
    backtest -> strategy, risk, report
"""

__version__ = "4.0.0"
