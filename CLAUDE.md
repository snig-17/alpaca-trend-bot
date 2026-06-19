# CLAUDE.md — working notes for Claude Code

This file is the durable, in-repo record of what this project is, how to run it,
and the rules that must not be broken. Read it first every session. Keep it
updated when something material changes.

## What this is
A **paper-trading** bot in Python. Cross-asset **daily trend-following** over 9 ETFs
+ 1 crypto, spanning 6 correlation clusters:

- **SPY**, **EFA** — equity (US, international)
- **IEF**, **TLT** — rates (intermediate, long Treasuries)
- **GLD** — metals
- **DBC**, **DBA** — commodity (broad/energy-heavy, agriculture)
- **UUP**, **FXY** — currency (US dollar, Japanese yen)
- **BTC/USD** — crypto (24/7, long-only spot, high cost ~15bps, ATR-sized small)

Breadth (currency/commodity/rates depth + crypto) was added 2026-06-19 — the trend
premium is largely a diversification effect; see `docs/research-trend-following.md`.
To actually trade BTC, crypto must be enabled on the Alpaca paper account; until
then its order fails gracefully (logged, never crashes the run).

Uses the **alpaca-py** SDK (NOT the deprecated `alpaca-trade-api`). Deterministic
rules only — **no LLM in the trade loop**. Paper account by default. Paper money,
not financial advice.

## Canonical location & environment
- **Repo / working dir:** `~/trader` (this folder). This is the ONE real copy.
  - It was consolidated here off the iCloud-synced Desktop to avoid sync stalls
    and a split venv. Old copies (`~/Desktop/trader2`, `~/Desktop/trading-bot`)
    have been removed. `~/Desktop/trader` is a *different, unrelated older
    project* — do not confuse it with this one.
- **venv:** `~/trader/.venv` (Python 3.13.5). Packages: alpaca-py, yfinance,
  pandas, numpy, scipy, matplotlib.

## How to run
```bash
cd ~/trader
source .venv/bin/activate
set -a; source .env; set +a        # loads Alpaca PAPER keys (config.py also auto-loads .env)

python bot.py dry     # pull live equity, compute targets, place NOTHING (market-hours independent)
python bot.py once    # one rebalance pass; only fills during US market hours (09:30–16:00 ET)
python test_safety.py        # 11 safety checks — all must PASS
python validate_for_live.py  # walk-forward validation; writes validation_reports/trend.json
```
- Outside US market hours, `bot.py once` correctly logs `market_open: False` and
  places 0 orders. That is expected, not a bug.

## Current status (last verified 2026-06-19)
- `test_safety.py` — all 11 checks PASS (9-name universe; the gross-cap test is now
  rounding-aware since summing 9 display-rounded targets drifts ~3e-4).
- `validate_for_live.py` — PASSES on the 10-instrument universe (9 ETFs + BTC),
  now validating the EXACT strategy the bot trades (long-only, allow_short=False):
  in-sample Sharpe 0.83, out-of-sample 1.06, deflated Sharpe 0.9997 over 19 trials
  (winner fast=50, slow=150). Report in `validation_reports/trend.json`.
  - GAP FIXED (2026-06-19): previously validation ran long/SHORT (trend_signal default
    allow_short=True) while the bot runs long-only, AND the bot used config 50/200 while
    validation reported a different grid winner. Now: validate_for_live carries the
    runtime trend config (so it tests long-only), and bot.runtime_params() overlays the
    validated winner fast/slow from the report — so the gate certifies what actually
    trades. Long-only validated BETTER than long/short (shorting these trends lost money).
  - HONEST NOTE on BTC: the strong OOS number is materially inflated by BTC's 2017/
    2020-21 bull runs landing in the OOS half (long-only crypto catches moonshots, never
    shorts crashes) — do NOT read 1.06 as a forecast. Crypto is high-vol, drawdown-heavy
    (>-70% bears); ATR sizing keeps the position small; it only trades when BTC's daily
    trend is positive.
  - HONEST NOTE on 5->9 ETFs: was ~Sharpe-neutral (diversification, not alpha).
  - Do NOT tune the grid to manufacture a passing number.
- `bot.py dry` / `bot.py once` — connect to Alpaca paper fine (equity $100k).

## Strategy summary
- Default strategy: **trend** (EMA crossover), **long-only**, daily bars.
- Three families exist in `strategies.py` (mean_reversion, breakout, trend);
  only **trend** has passed validation and may approach live.
- Sizing: **ATR volatility targeting** — risk ~1% of equity per position, stop at
  `k_stop * ATR`; quiet names get bigger positions, wild names smaller, so dollar
  risk is roughly constant across instruments.

## Risk limits (config.py → RiskConfig)
| Limit | Value |
|---|---|
| max_risk_per_trade | 1% |
| k_stop (ATR multiples) | 3.0 |
| max_position_pct (per name) | 25% |
| max_gross_leverage | 1.5x |
| per_group_gross_cap (correlated cluster) | 60% |
| daily_loss_limit_pct (halt + flatten) | 3% |
| max_drawdown_pct (halt + flatten) | 10% |

## HARD GUARDRAILS — do not weaken
1. **PAPER by default.** Live is gated behind ALL of: `BOT_MODE=LIVE` +
   `I_UNDERSTAND_LIVE_RISK=YES` + a `LIVE_AUTHORIZED` file + a *passing, fresh*
   validation report (`VALIDATION_MIN_DSR=0.95`, `VALIDATION_MAX_AGE_DAYS=30`).
   Never bypass or soften this gate.
2. **broker.py is hardened:** 12s timeout on every Alpaca call; a timed-out order
   is flagged ambiguous and is **NOT** auto-resent (it may have hit the market).
   Do not add blind retries.
3. **risk.py stays intact:** ATR sizing + per-name/per-cluster/gross caps + daily
   loss & drawdown breakers + `KILL`/`HALTED` files.
4. **Validation-first.** A strategy must pass `validate_for_live.py` before going
   near live. **Never tune parameters just to manufacture a passing Sharpe.**

## Secrets
- `.env` holds Alpaca **PAPER** keys. It is **gitignored** — never commit, print,
  or push it. `.env.example` is the safe template that IS committed.
- If a run says keys are missing: `set -a; source .env; set +a`.

## File map
- `bot.py` — entry point / run loop (`dry`, `once`).
- `strategies.py` — signal families + ATR vol-target sizing.
- `risk.py` — risk manager: sizing caps + circuit breakers.
- `config.py` — mode, universe, RiskConfig, paths, live-gate thresholds.
- `broker.py` — Alpaca wrapper (hardened, timeouts, no blind retries).
- `data.py` — bar data (yfinance).
- `costs.py` — transaction-cost model (bps per unit turnover).
- `metrics.py`, `validation.py`, `validate_for_live.py` — backtest stats +
  walk-forward validation gate.
- `backtest.py`, `preflight.py`, `journal.py` — backtest harness, pre-run checks,
  trade/PNL journaling.
- `test_safety.py` — safety test suite.
- `state/`, `journal/`, `validation_reports/` — runtime outputs.
- Manual safety files (create by hand): `KILL` (halt now), `LIVE_AUTHORIZED`
  (one of three live gates), `state/HALTED` (written when a breaker trips).

## Open items / decisions in flight
- **Strategy direction (RESOLVED 2026-06-19):** user wanted "many small bets for
  small accumulating returns." Sound version = more breadth (uncorrelated markets),
  NOT higher trade frequency (turnover costs kill thin-edge fast strategies).
  Implemented breadth (5->9 ETFs), revalidated (PASS), and decided to KEEP it (see
  Current status). Research digest + re-validation now automated monthly.
- **Possible next levers (not yet done):** smaller per-trade risk (lower
  max_risk_per_trade / position caps) for an even calmer ride; or installing the
  local launchd agents (user action). Any strategy change must re-pass
  `validate_for_live.py` — never tune to manufacture a pass.

## Automation (set up 2026-06-19)
The optimal workflow for a daily-bar trend bot is **reliable execution + slow,
disciplined re-validation + monitoring** — NOT picking a new "optimal strategy"
each day (that is overfitting/regime-chasing and is what blows accounts up). No LLM
in the trade loop. Two layers, split by where they CAN run:

- **Cloud (Claude scheduled routine — manage at https://claude.ai/code/routines):**
  runs in Anthropic's cloud from the GitHub repo. Reports via a **GitHub issue**
  (Gmail was dropped — connector only drafts + token expiry; cloud also blocks
  Yahoo Finance egress and PushNotification was unreliable).
  - `trader — monthly trend-following research digest` (1st, 09:00 UTC, Opus):
    web-researches trend developments, files a cited issue (human-review input
    only). Web access works in the sandbox.
  - NOTE: cloud `trader — monthly re-validation` (trig_015i…) CANNOT fetch market
    data (egress blocks yfinance, 403) — re-validation moved LOCAL. Disable or
    leave it filing RUN-ERROR issues; egress allowlist on the Default env is not
    user-editable.
- **Local (`automation/` via launchd — needs `.env` keys / live market data):**
  - `bot.py once` daily weekdays ~15:00 London (~10:00 ET).
  - `monitor.py` after close ~21:15 London: equity/positions/P&L/drawdown +
    HALT/KILL → `logs/` + macOS notification.
  - `validate_for_live.py` monthly, 1st 09:00 London → `logs/` + notification
    (PASS / EDGE DEGRADED). yfinance works locally.
  - Install steps + VPS reliability caveat: `automation/README.md`. NOT auto-loaded;
    user installs the LaunchAgents. macOS notifications use ASCII text only (AppleScript
    can't parse JSON \uXXXX escapes, so no emoji/em-dash in notification strings).

## Standing context
Plan is to paper-trade for months before drawing conclusions. Early P&L is noise.
A passing validation is necessary but not sufficient.
