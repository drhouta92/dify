# Swing Bot v3: Review and v4 Rewrite Rationale

The review covers all 16 files of `swing_bot_code_ordered.zip`. Each finding names the v3 location, the way it fails, and the v4 fix. I checked library behaviour against the installed source of **alpaca-py 0.45.0** and **tvscreener 0.8.2**, not from memory.

Severity scale: **Critical** means the bot can lose protection, trade on invented data, or halt for good. **High** means it can bias results or carry unmanaged risk. **Medium** covers robustness and operations. **Low** covers hygiene.

---

## 1. Critical defects

| # | v3 location | Failure mode | v4 fix |
|---|---|---|---|
| C1 | `paper.py` `_ensure_stop`: `client_order_id = sha256(symbol\|entry_time\|stop)` | **The ID is deterministic and gets reused.** Alpaca requires each `client_order_id` to be unique and rejects a duplicate with a 4xx. (a) Say the first stop submission reaches the broker but its response is lost. The retry reuses the same ID and is rejected, so all attempts "fail" and the new position is flattened with `EXIT_UNPROTECTED` while the live stop still holds its shares. The market sell is then rejected for insufficient quantity, `pending_exit_order_id` stays `None`, and the position is stuck for good. (b) `_restore_stop` after a failed exit, with the stop unchanged, regenerates the same ID. The stop can never be re-created and the position stays **unprotected**. | Every ID is `sb4-<purpose>-<symbol>-<position>-<seq>`, where `seq` is a persisted counter that only increases (`execution.py` I2). A failed submit is looked up by its ID before anything else happens (`alpaca_broker._submit`). |
| C2 | `paper.py` `reconcile_entry_ledger` only visits `status == 'pending'` | **Uncertain buys are never reconciled.** `open_position` marks a lost response as `released_unknown`. If that order filled, the shares sit at the broker with no stop and are logged as *"Broker holds X which this bot does not manage (ignored)"*. | An order whose outcome is unknown stays an unresolved *intent*. Every cycle looks it up by ID: if found, it is booked and protected; if not found after a grace period, it is marked absent (`execution._resolve`, `settle_entry`). |
| C3 | `alpaca_client.submit_order` turns *every* exception into `None` | **One rejection halts all entries.** A definite 4xx ("not tradable", "insufficient buying power") looks the same as a timeout. The bot then sets `released_unknown`, which blocks all entries until an operator sets `SWING_ACK_UNKNOWN_ENTRIES=1` and restarts. That flag then stays latched and acknowledges every *future* unknown order too. | Errors are classified into three types (`broker.py`): `OrderRejected` voids that one position and the bot continues; `BrokerUnavailable` is resolved automatically; `OrderNotFound` is definite. The env-var acknowledgement is gone. |
| C4 | `alpaca_client.py` / `config.AlpacaConfig.TIMEOUT` | **There are no HTTP timeouts.** alpaca-py's `RESTClient._one_request` calls `session.request(method, url, **opts)` without a timeout (checked in the 0.45.0 source), and `TIMEOUT=30` is never read. One half-open TCP connection freezes the trading loop: no exits and no stop ratchets. | A `(5 s, 20 s)` timeout is put on every alpaca-py session (`alpaca_broker._install_timeout`). TradingView, yaaqen and Shariah requests also carry timeouts. |
| C5 | `paper.py` `close_position`, `_check_stop_health`, `apply_update` | **A stop that was replaced strands the bot.** If a `replace_stop` succeeded but its response was lost, `stop_order_id` points at an order with status `replaced`. `_check_stop_health` treats it as healthy. `close_position` cannot cancel it, sees status `replaced` (not in the cancelled set), and refuses to sell on every cycle, so **strategy exits never execute**. When the new stop fills, the trade is booked as `EXIT_UNKNOWN`. | The bot follows `replaced_by` chains (I6). A replacement gets its own write-ahead ID. The stop price of the live broker stop becomes the authoritative `pos.stop`. |
| C6 | `paper.py` `sync_with_broker` → `_record_exit(..., p['stop'], 'EXIT_UNKNOWN')` | **The exit price is invented.** A position that vanished at the broker is booked at its stop price. That P&L goes into `realized_pnl`, then into `sizing_equity()`, so the next position is sized on equity that does not exist. | Accounting is recomputed from broker fills only (I4). A quantity the bot cannot explain quarantines that one symbol (I5); no price is ever made up. `quarantine-clear` reconciles it and excludes it from statistics. |

## 2. High-severity defects

| # | v3 location | Problem | v4 fix |
|---|---|---|---|
| H1 | whole kit | **The strategy was never validated.** There is no backtest. The 2-ATR stop, 3R target, 26-bar breakout and the 40/25/20/−10/−5 optimizer weights are untested choices. | `backtest.py` runs the *same* `strategy.evaluate_entry` / `manage_position` and `risk.*` functions with the live scheduler's exact timing. Tests confirm it has no look-ahead and that costs reduce P&L. |
| H2 | `signal_engine.analyze_entry` `strong_body = abs(close-open)/range` | **Bearish breakout bars pass.** A gap-up bar that closes lower than its open but above the prior high counts as a "strong body". | The body must be bullish: `(close − open)/range ≥ 0.40` (test `test_bearish_breakout_bar_rejected`). |
| H3 | `paper.py` `evaluate_position` → `EXIT_TP` | **The take-profit cannot fill as modelled.** The target is detected on a *closed* bar's high and then sent as a market order. Price has often retraced by then, so the realized TP is systematically below the target. | The target rests at the broker as an **OCO** (take-profit limit plus stop leg, GTC). One fill cancels the other. |
| H4 | `technical_filter.BellFilter` | **The Amihud measure is misspecified.** It uses the intraday open→close return instead of close-to-close (Amihud 2002), with IEX volume scaled by a fixed 3%. IEX's share of volume varies a lot by symbol and day. | Close-to-close returns on **consolidated SIP daily bars**, which Alpaca serves free once they are more than 15 minutes old. IEX scaling is kept only as a logged fallback (`liquidity.py`). |
| H5 | discovery / signal | **Earnings risk is unmanaged.** Positions are held up to 5 sessions with no earnings blackout. An earnings gap skips the stop, which fills at the gap price. This is the largest tail risk in a swing book. | The universe excludes names reporting within 8 calendar days. Held positions get an `EXIT_EVENT` on the last bar before earnings (`earnings_exit_business_days`). |
| H6 | `paper.py` | **There are no portfolio loss controls.** No daily loss limit, no drawdown halt, no PDT guard. With `SIZING_EQUITY=1000` and real-account intent, a same-day stop-out counts as a day trade, and four in five days flags PDT. | `risk.entry_gate`: daily loss limit, persistent drawdown halt (`reset-halt`), PDT guard, position and trade caps, and a block on unresolved orders. |
| H7 | `paper.run_market_loop` → `refresh_universe()` inside the first 09:45 cycle | **The universe build delays the first decision.** About 100 web scrapes plus daily fetches run before the first decision. Ranking uses a 15-minute relative-volume reading. If the build fails, it is retried every 30 s, hammering TradingView and yaaqen. | The universe is built **before the open** (`universe_build_lead_min`) with a 5-minute retry backoff. A failed build never blocks position management. |
| H8 | `signal_engine.resample_1h`, `evaluate_position` time exit | **The decisions depend on whether data exists.** On IEX a quiet 15m interval has no bar. A complete hour is then dropped because "the last 15m bar is missing". The time exit (`_mins(df15.index[-1]) >= 15:30`) does not fire if the 15:30 bar has no prints, so the hold runs past the maximum. | Hourly bins are complete **by the clock** (`bin_end ≤ asof`). Exit timing uses the expected `bar_start`, not the last bar present (`test_hourly_bins_complete_by_clock_not_by_data`). |

## 3. Medium / low

| # | Problem | v4 fix |
|---|---|---|
| M1 | JSON state rewritten on every change **without fsync**. A power loss can leave a renamed but empty file. The stats tool reads while the writer replaces the file. | SQLite with WAL and `synchronous=FULL`, multi-row transactions, and an append-only `events` journal. The backup uses the SQLite online-backup API. |
| M2 | The account lock is held during network waits of 45 s or more, which freezes the dashboard. `run_service.py` monkey-patches `get_clock`, `fetch_15m` and `run_cycle`. | No monkey-patching. Health is written by the trader, and `status` reads SQLite without taking locks. |
| M3 | `adjustment='all'` on intraday bars: after an ex-dividend date, history is rewritten, so the HWM and trailing stop no longer match real fill prices. | `Adjustment.SPLIT`. |
| M4 | The optimizer runs an exhaustive `combinations()` search for K = 1. Its score mixes uncalibrated weights with terms the filters already enforce. | Candidates are ranked by **entry cost in R units**, the one quantity that reduces expected R by construction, then by relative volume. Candidates with cost_R > `max_cost_r` are rejected (`risk.py`). |
| M5 | The cooldown is 48 wall-clock hours, so a Friday exit expires over the weekend. | The cooldown counts sessions (`np.busday_count`). |
| M6 | `deployable()` can go negative when `SIZING_EQUITY ≤ 0`. Sizing ignores open losses. | Cash floor at 0. The gate includes unrealized P&L. |
| M7 | The Shariah cache is wiped every day (about 100 scrapes a day). The scraper spoofs a browser User-Agent and runs 10 concurrent workers against a small site. | 7-day TTL cache, honest configurable User-Agent, 4 workers, backoff on 429/5xx, and an operator overrides file. The fail-closed semantics are kept. |
| M8 | `stats_report` reports a per-trade "Sharpe-ish" with no uncertainty. | R-multiples, expectancy with a **bootstrap 95% CI**, an "edge established" flag, and breakdowns by exit reason and entry time. |
| M9 | A failed bar chunk silently drops up to 50 symbols, held ones included. | The error is classified and the cycle is aborted (exits are retried on the next bar while broker stops protect). Missing held data is logged explicitly. |
| L1 | `SIZING_EQUITY` and `MAX_TRADES_PER_DAY` are parsed at *import time* in dataclass defaults, so a bad value crashes the import. | All configuration is parsed and validated in `Settings.from_env()`, with clear errors. |
| L2 | `paper.py` (about 800 lines) mixes accounting, execution, sync and scheduling. It also mixes pytz and zoneinfo, and has a capitalised `Shariah.py`. | Layered package: strategy and indicators (pure) / risk / execution / store / broker adapter / trader / CLI. |

### Kept from v3, because it was sound
- Paper-only enforcement, now validated in configuration.
- Recording the order intent before network I/O.
- Quarantine instead of crashing.
- Fail-closed Shariah screening.
- Decisions on closed bars only.
- systemd hardening, now tighter.
- A logrotate policy that never touches state files.

---

## 4. Scientific framing

### 4.1 Unit of account: R
For a trade with entry $P_e$, initial stop $S_0$ and $q$ shares, $1R = q\,(P_e - S_0)$ and $R_i = \text{PnL}_i / 1R$. Expectancy is

$$\mathbb{E}[R] = p\,\bar W_R - (1-p)\,\bar L_R - c_R,$$

where $c_R$ is the round-trip cost in R. For entries,

$$c_R^{entry} = \frac{\text{limit} - \text{mid}}{k \cdot ATR_d}.$$

Worked example: a \$10 stock with ATR = 3% has 1R = \$0.60 at k = 2. A 0.2% spread plus the 0.3% limit allowance costs up to \$0.04 = **0.067 R** on entry alone. That is why v4 ranks and gates candidates on $c_R$ instead of an ad-hoc score.

### 4.2 How many trades prove an edge?
A one-sided test of $\mathbb{E}[R] > 0$ at 95% confidence needs about

$$n \approx \left(\frac{z_{0.95}\,\sigma_R}{\mu_R}\right)^2.$$

With typical breakout values $\sigma_R \approx 1.2$ and $\mu_R = 0.10$:

$$n \approx (1.645 \cdot 1.2 / 0.1)^2 \approx 390 \text{ trades}.$$

At `MAX_TRADES_PER_DAY=1`, paper trading alone would take more than 1.5 years. **The backtest is the primary validation instrument. Paper trading measures execution (slippage, fill rate), not the edge.**

### 4.3 Validation protocol (pre-register before looking at results)
1. **Data:** SIP 15m and daily bars, at least 2 years. Fix the symbol list *before* the test window. (Survivorship bias remains: say so in the write-up.)
2. **Split:** in-sample for any tuning, then a contiguous untouched out-of-sample (OOS) block of at least 6 months.
3. **Acceptance:** in OOS, at least 200 trades, a bootstrap 95% CI lower bound of mean R above 0 *after* costs (`--half-spread`, `--slip` stressed ×2), and max mark-to-market drawdown under the halt threshold.
4. **Robustness:** perturb each of `stop_atr_mult`, `take_profit_r`, `breakout_lookback`, `trail_atr_mult_1h` and `body_ratio_min` by ±20%. The sign of mean R must not flip. If it does, the edge is a parameter artefact.
5. **Paper phase:** run 50 or more trades. Compare live slippage (`entry_slippage_pct`, exit vs. stop) with the backtest `CostModel`, then recalibrate costs, not signals.

### 4.4 Known residual biases
- The backtest universe is chosen today (survivorship).
- Shariah status and earnings dates are not point-in-time.
- Fills ignore queue position and partial fills.
- IEX 15m bars (live) differ from SIP 15m bars (backtest) in volume and in which bars exist. The relative-volume filter therefore behaves differently live. Measure this in the paper phase before trusting live results.
