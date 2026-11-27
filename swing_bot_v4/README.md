# swingbot v4 (Alpaca PAPER)

Shariah-screened swing trading on 15m breakouts inside 1h and daily uptrends. Stops and take-profits rest at the broker as an OCO. A position is held for at most 5 sessions. **Paper endpoint only**: the configuration refuses any other URL.

See [REVIEW.md](REVIEW.md) for the v3 defects this version fixes and for the validation protocol.

## Layout

```
swingbot/
  config.py         typed, validated settings; every field overridable via SWING_<FIELD>
  indicators.py     causal EMA / Wilder ATR / rolling VWAP / clock-complete session 1h bins
  strategy.py       pure entry + position-management rules (shared by live and backtest)
  risk.py           sizing, cost-in-R ranking, portfolio entry gate (loss limits, PDT, caps)
  broker.py         broker-neutral types + error taxonomy (rejected / not found / unknown)
  alpaca_broker.py  alpaca-py 0.45.0 adapter with timeouts and idempotent submits
  store.py          SQLite (WAL) positions, write-ahead order intents, events journal
  execution.py      order state machine: entry, OCO protection, stop ratchet, exits, reconcile
  shariah.py        yaaqen.com ruling classifier, TTL cache, operator overrides
  liquidity.py      Amihud (close-to-close, SIP) + sell-trap + dollar-volume screen
  universe.py       pre-open universe: TradingView -> earnings blackout -> Shariah -> liquidity
  trader.py         scheduler + per-bar cycle + health.json
  backtest.py       event-driven backtest calling the same strategy/risk code
  report.py         R-multiple statistics with bootstrap confidence intervals
  cli.py            run | status | report | backtest | backup | quarantine-clear | reset-halt
deploy/             systemd unit, logrotate, env template, Ubuntu installer
tests/              FakeBroker reproducing Alpaca semantics + strategy/backtest tests
```

## Local run

```bash
python3.13 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
python -m pytest                                  # 43 tests, no network

export ALPACA_API_KEY=... ALPACA_SECRET_KEY=...   # PAPER keys
export SWING_DATA_DIR=./var
python -m swingbot backtest --symbols AAPL,MSFT,AMD --start 2025-01-02 --end 2025-12-31 --out trades.csv
python -m swingbot run
python -m swingbot status
python -m swingbot report
```

## Server (Ubuntu 22.04/24.04)

```bash
sudo bash deploy/setup.sh
sudo nano /etc/swing-bot.env          # paste PAPER keys
sudo systemctl restart swing-bot
sudo journalctl -u swing-bot -f
sudo -u swing-bot env SWING_DATA_DIR=/var/lib/swing-bot /opt/swing-bot/venv/bin/python -m swingbot status
```

v4 does not import v3's `paper_state.json`. Any v3 positions still open at Alpaca show up as *untracked*: v4 never trades them, but they count against position slots. Close or manage them by hand.

## Operator runbook

| Signal | Meaning | Action |
|---|---|---|
| `QUARANTINE SYM` | The broker quantity does not match the bot's fills. | Inspect orders at Alpaca, then run `swingbot quarantine-clear SYM`: if the broker is flat, the trade is closed and excluded from statistics; otherwise the bot adopts the broker quantity and re-protects. |
| `order outcome UNKNOWN` | A network failure happened during a submit. | Nothing to do. It resolves by `client_order_id`, or is marked absent after `SWING_UNKNOWN_ORDER_GRACE_MIN`. |
| `UNPROTECTED SYM` | A protective order could not be confirmed. | It is retried every cycle. A new entry that cannot be protected is flattened automatically. |
| `DRAWDOWN HALT` | Equity fell more than `max_drawdown_pct` below its peak. | Investigate, then run `swingbot reset-halt`. |
| `HEARTBEAT` | No successful cycle for 30 minutes during market hours. | Check `journalctl`, network and the Alpaca status page. Broker-side stops still protect open positions. |

## Shariah overrides

To apply an authority's ruling without code changes, create `$SWING_DATA_DIR/shariah_overrides.json`:

```json
{"allow": ["TICKER1"], "deny": ["TICKER2"]}
```

`deny` wins over `allow`. A status of unknown never permits an entry and never forces an exit.
