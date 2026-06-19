"""
bot.py
======
The orchestration loop. One pass = one daily rebalance:

    preflight gates -> circuit breakers -> (market open?) -> bars -> signals
    -> risk-sized targets -> reconcile vs current positions -> journal -> trade

Designed for a daily-bar cadence run once per day via cron/systemd (more robust
than an in-process sleep loop). Trading decisions are deterministic rules; no LLM
sits in this loop.

CLI:
    python bot.py once          # one paper rebalance (real Alpaca paper account)
    python bot.py dry            # compute + journal intended trades, place none
"""

from __future__ import annotations
import sys
import os
import json
import datetime as dt

import config
import journal as J
import preflight
from preflight import TradingHalted
from risk import RiskManager
from broker import BrokerBase, AlpacaBroker
import strategies as S
import data as engine_data

_STRATEGY_FN = {
    "trend": S.trend_signal,
    "mean_reversion": S.mean_reversion_signal,
    "breakout": S.breakout_signal,
}
MAX_CONSECUTIVE_ERRORS = 3


def make_broker() -> BrokerBase:
    paper = (config.MODE == "PAPER")
    return AlpacaBroker(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=paper)


def runtime_params(strategy: str) -> dict:
    """The exact params the bot trades. For 'trend', overlay the validated winner
    (fast/slow) from the latest validation report so the bot trades precisely what
    passed the live gate; everything else (e.g. allow_short) stays from config.
    Falls back to config defaults if no report exists."""
    params = dict(config.STRATEGY_PARAMS.get(strategy, {}))
    if strategy == "trend":
        try:
            with open(os.path.join(config.VALIDATION_DIR, f"{strategy}.json")) as f:
                winner = (json.load(f) or {}).get("winner_params") or {}
            for k in ("fast", "slow"):
                if k in winner:
                    params[k] = winner[k]
        except Exception:
            pass
    return params


def compute_signal_atr(df, strategy: str):
    fn = _STRATEGY_FN[strategy]
    params = runtime_params(strategy)
    sig = fn(df, **params)
    last_sig = float(sig.iloc[-1]) if len(sig) and sig.iloc[-1] == sig.iloc[-1] else 0.0
    a = S.atr(df, config.RISK.atr_n)
    price = float(df["close"].iloc[-1])
    atr_frac = float(a.iloc[-1] / price) if a.iloc[-1] == a.iloc[-1] and price else None
    return last_sig, atr_frac, price


def _rationale(strategy: str, sig: float, target_frac: float) -> str:
    direction = "long" if sig > 0 else ("short" if sig < 0 else "flat")
    desc = {
        "trend": "50/200 EMA trend",
        "mean_reversion": "z-score mean reversion",
        "breakout": "Donchian breakout",
    }.get(strategy, strategy)
    return f"{desc}: signal={direction}; target {target_frac:+.1%} of equity"


def run_once(broker: BrokerBase, dry_run: bool = False, min_rebalance_frac: float = 0.005) -> dict:
    state = J.load_state()

    # 1. Gates that can stop us entirely
    preflight.assert_can_trade(config.STRATEGY)

    # 2. Circuit breakers -- evaluated before doing anything
    equity = broker.get_equity()
    halt, reasons = RiskManager(config.RISK).breaker_check(equity, state)
    if halt:
        J.journal_pnl(equity, state.get("day_pl", 0.0), state.get("drawdown", 0.0))
        if not dry_run:
            broker.cancel_all_orders()
            broker.close_all()
        preflight.write_halt(reasons)
        J.save_state(state)
        return {"halted": reasons, "equity": equity, "action": "flattened"}

    market_open = broker.is_market_open()

    # 3. Signals + sizing
    rm = RiskManager(config.RISK)
    signals, atr_frac, prices, groups, errors = {}, {}, {}, {}, {}
    for inst in config.UNIVERSE:
        try:
            df = engine_data.load(inst.yf, period="2y")
            s, a, p = compute_signal_atr(df, config.STRATEGY)
            signals[inst.name], atr_frac[inst.name], prices[inst.name] = s, a, p
            groups[inst.name] = inst.group
        except Exception as e:  # bad fetch -> skip this name, don't crash the book
            errors[inst.name] = str(e)

    targets = rm.target_exposures(signals, atr_frac, groups)

    positions = broker.get_positions()
    alpaca_of = {i.name: i.alpaca for i in config.UNIVERSE}
    is_crypto = {i.name: (i.asset_class == "crypto") for i in config.UNIVERSE}

    # 4. Reconcile to targets; journal intent BEFORE trading
    rows, orders = [], []
    for name, tfrac in targets.items():
        price = prices.get(name)
        if not price:
            continue
        sym = alpaca_of[name]
        cur_notional = positions[sym].market_value if sym in positions else 0.0
        tgt_notional = tfrac * equity
        delta = tgt_notional - cur_notional
        tradable_now = market_open or is_crypto[name]

        if not tradable_now:
            action = "SKIP_MARKET_CLOSED"
        elif abs(delta) < min_rebalance_frac * equity:
            action = "HOLD"
        else:
            action = "BUY" if delta > 0 else "SELL"

        rows.append({
            "symbol": sym, "signal": signals.get(name, 0.0),
            "current_expo": round(cur_notional / equity, 4) if equity else 0.0,
            "target_expo": round(tfrac, 4), "delta_notional": round(delta, 2),
            "action": action, "price": round(price, 4),
            "rationale": _rationale(config.STRATEGY, signals.get(name, 0.0), tfrac),
        })
        if action in ("BUY", "SELL"):
            orders.append((sym, abs(delta) / price, "buy" if delta > 0 else "sell"))

    J.journal_decision(rows)

    # 5. Execute (paper/live). Mock/dry runs skip real sends.
    placed = []
    if not dry_run:
        for sym, qty, side in orders:
            placed.append(broker.submit_market_order(sym, qty, side))

    state["consecutive_errors"] = 0
    J.journal_pnl(equity, state.get("day_pl", 0.0), state.get("drawdown", 0.0))
    J.save_state(state)
    return {
        "mode": config.MODE, "equity": round(equity, 2), "market_open": market_open,
        "targets": {k: round(v, 4) for k, v in targets.items()},
        "orders_planned": len(orders), "orders_placed": len(placed),
        "data_errors": errors, "dry_run": dry_run,
    }


def safe_run_once(broker: BrokerBase, dry_run: bool = False) -> dict:
    """Wrap run_once so repeated failures HALT instead of hammering the broker."""
    state = J.load_state()
    try:
        result = run_once(broker, dry_run=dry_run)
        return result
    except TradingHalted as e:
        return {"halted_preflight": str(e)}
    except Exception as e:
        n = state.get("consecutive_errors", 0) + 1
        state["consecutive_errors"] = n
        J.save_state(state)
        if n >= MAX_CONSECUTIVE_ERRORS:
            preflight.write_halt([f"{n} consecutive errors; last: {e}"])
            return {"halted": [f"{n} consecutive errors"], "last_error": str(e)}
        return {"error": str(e), "consecutive_errors": n}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dry"
    b = make_broker()
    out = safe_run_once(b, dry_run=(cmd == "dry"))
    print(dt.datetime.now().isoformat(timespec="seconds"), out)
