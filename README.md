# Systematic Trading Bot (paper-first, safe by construction)

A deterministic, daily-rebalanced trading bot for Alpaca. It is built to be safe
by construction: it trades paper money by default, refuses to touch real money on
a strategy that hasn't passed validation, sizes every position through a hard risk
layer, and halts itself when things go wrong.

Two design decisions matter most:

- **No LLM in the trading loop.** Every trade comes from deterministic rules you
  can read and audit. Claude helped *build and validate* this; it does not make
  live decisions. That removes the "the model hallucinated and liquidated the
  account" failure mode entirely.
- **Validation is a precondition for live, enforced in code.** You cannot flip a
  switch and trade real money on an untested idea — the bot checks for a passing
  validation report and refuses otherwise.

### Read this first
This will not reliably make you money, and nothing here is investment advice. The
honest evidence (see the companion research framework) is that most retail
strategies have no edge after costs. The one strategy here that clears the
validation bar — cross-asset trend following — has a *modest* out-of-sample Sharpe
(~0.5), not a spectacular one, and that figure will erode further with real-world
frictions and taxes. **Clearing the gate means "worth paper-trading for months,"
not "fund it now."** Trade only money you could lose entirely.

---

## Safety model

| Layer | What it does |
|-------|--------------|
| **PAPER default** | `BOT_MODE=PAPER` uses Alpaca's fake-money sandbox. This is where you should stay for months. |
| **Three-gate live** | LIVE requires *all* of: `BOT_MODE=LIVE` + `I_UNDERSTAND_LIVE_RISK=YES`; a hand-created `LIVE_AUTHORIZED` token file; and a fresh, passing validation report for the strategy. |
| **Volatility-targeted sizing** | ATR-based; risk per position is roughly constant across instruments. |
| **Hard caps** | Per-name cap, per-correlated-cluster cap (so SPY+EFA can't secretly become one huge bet), and a gross-leverage cap — enforced regardless of what the strategy wants. |
| **Daily-loss killswitch** | If the day's P&L breaches −3%, the bot cancels orders, flattens the book, and halts. The day's baseline is the broker's *previous close* (Alpaca `last_equity`), so it works for a bot that runs once a day. |
| **Max-drawdown breaker** | If equity falls 10% from its peak, same: flatten and halt. |
| **`KILL` file** | Create a file named `KILL` in the folder to halt instantly, any mode. |
| **`HALTED` file** | Written automatically when a breaker trips; the bot refuses to resume until a human reviews and deletes it. |
| **Consecutive-error halt** | Repeated failures stop the bot instead of letting it hammer the broker. |
| **Journaling** | Every decision is logged *with its rationale before the order is sent* (`journal/decisions_*.csv`), plus daily P&L. |

All of the above is verified by `python test_safety.py`, which drives the bot
through each scenario using a mock broker — no credentials, no network, no risk.

---

## The live-gate, concretely

`validate_for_live.py` grids the strategy's parameters, builds the equal-weight
volatility-targeted portfolio, splits history into in-sample / out-of-sample,
and computes the **Deflated Sharpe Ratio** (correcting for how many parameter sets
were tried). It writes `validation_reports/<strategy>.json`. The bot will only
permit LIVE mode if that report has `deflated_sharpe >= 0.95`, a positive
out-of-sample Sharpe, and is less than 30 days old.

A caveat the green light does not capture: the deflated Sharpe here counts the
parameter grid, but not every researcher degree of freedom (universe choice,
sizing rules, cost assumptions, rebalance cadence). The true significance is
therefore *lower* than the number in the report. Treat a pass as necessary, not
sufficient — and keep paper-trading.

---

## Files

| File | Role |
|------|------|
| `config.py` | Mode (PAPER default), risk limits, instrument universe, live-gate thresholds |
| `broker.py` | Alpaca adapter (`alpaca-py`) + in-memory `MockBroker` for tests |
| `risk.py` | Sizing, caps, correlation filter, circuit breakers |
| `preflight.py` | Safety gates + three-gate live authorization |
| `journal.py` | State persistence + decision/P&L journals |
| `bot.py` | The rebalance loop (preflight → breakers → signals → sizing → reconcile → journal → trade) |
| `validate_for_live.py` | Walk-forward + deflated Sharpe; writes the gate's report |
| `test_safety.py` | Proves every guardrail fires (mock broker) |
| `strategies.py`, `metrics.py`, `backtest.py`, `data.py`, `costs.py`, `validation.py` | Reused research/validation engine |

---

## Setup & run

```bash
pip install alpaca-py yfinance pandas numpy scipy matplotlib

# 1. Prove the safety machinery works (no keys needed):
python test_safety.py

# 2. Get free Alpaca PAPER keys at app.alpaca.markets, then:
cp .env.example .env        # fill in your PAPER keys; leave BOT_MODE=PAPER
set -a; . ./.env; set +a

# 3. Dry run (computes + journals intended trades, places none):
python bot.py dry

# 4. Paper rebalance (places fake-money orders on your Alpaca paper account):
python bot.py once

# 5. Generate the validation report (honest go/no-go evidence):
python validate_for_live.py
```

Run it once per trading day. The robust way is **cron** (a daily-bar bot does not
need an always-on process):

```cron
30 20 * * 1-5  cd /path/to/trading-bot && set -a && . ./.env && set +a && /usr/bin/python3 bot.py once >> bot.log 2>&1
```

Schedule it **inside** US market hours (13:30–20:00 UTC in EDT, 14:30–21:00 UTC in
EST) or the nine ETFs log `SKIP_MARKET_CLOSED` and only BTC rebalances. On a
best-effort scheduler such as GitHub Actions — whose runs can drift hours late —
schedule two early slots: `bot.py once` holds on the second one if the first
already rebalanced with the market open, and `bot.py once --force` overrides that
guard for a manual run. `.github/workflows/daily-trade.yml` does exactly this.

A $5–10/month VPS is fine. Use the `KILL` file as your emergency stop.

---

## Going live (only if you truly choose to)

Paper-trade for **at least a few months** and confirm live behaviour tracks the
backtest. Then, and only then, all three of these must be true:

1. `BOT_MODE=LIVE` and `I_UNDERSTAND_LIVE_RISK=YES` in your environment, with your
   **live** Alpaca keys.
2. A file named `LIVE_AUTHORIZED` created by hand in the project folder.
3. A current, passing `validation_reports/<strategy>.json`.

Start with an amount you can lose entirely. The risk caps mean ~1% of equity per
position and a 10% portfolio circuit breaker, but those limit damage; they do not
guarantee profit.

---

## Honest limitations

- **Daily cadence.** Signals and stops are evaluated once per day; the stop is
  enforced in-loop. For intraday trading you would want resting broker-side stop
  orders and tick data — and you would face more cost drag and more overfitting
  risk, not less.
- **Signal data is yfinance; execution is Alpaca.** Fine for daily bars; not for
  latency-sensitive strategies.
- **This universe is not "real" diversified trend.** True time-series momentum
  runs across dozens of futures; six ETFs is a gesture in that direction. The edge,
  such as it is, is modest and may not persist.
- **A passing validation is not a promise.** Edge that survives this gate can still
  die live. Markets change; backtests are not the future.

---

## Disclaimer

Educational software, not investment advice and not a solicitation to trade. Past
performance — backtested or live — does not predict future results. You are solely
responsible for any orders this software places. Trade only money you can afford to
lose entirely, and consult a licensed professional before risking real capital.
