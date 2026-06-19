"""
validate_for_live.py
====================
The live-gate in preflight.py refuses real money unless a fresh, PASSING
validation report exists for the configured strategy. This script produces that
report honestly: it grids the strategy's parameters, builds an equal-weight,
volatility-targeted portfolio across the universe, measures in-sample vs
out-of-sample Sharpe, and computes the Deflated Sharpe (correcting for the number
of parameter sets tried).

Run:  python validate_for_live.py

It writes validation_reports/<strategy>.json. If the strategy fails to clear the
bar (Deflated Sharpe >= threshold AND positive OOS Sharpe), the report is written
with passing=False and the live-gate stays shut -- which is the correct, honest
outcome for strategies that don't actually have an edge.
"""

from __future__ import annotations
import itertools
import json
import datetime as dt
import numpy as np
import pandas as pd

import config
import data as D
import metrics as M
import strategies as S
from backtest import run_backtest

SPLIT = 0.5
TREND_GRID = {"fast": (20, 30, 50, 75, 100), "slow": (100, 150, 200, 250)}


def _portfolio_net(params: dict) -> pd.Series:
    """Equal-weight, vol-targeted portfolio net returns across the universe."""
    # Validate the SAME strategy the bot trades: carry the runtime trend config
    # (notably allow_short) and let the grid only vary fast/slow. Otherwise the gate
    # would certify a long/short strategy the long-only bot never runs.
    trend_cfg = dict(config.STRATEGY_PARAMS.get("trend", {}))
    legs = {}
    for inst in config.UNIVERSE:
        try:
            df = D.load(inst.yf, period="max")
        except Exception:
            continue
        sig = S.trend_signal(df, **{**trend_cfg, **params})
        expo = S.vol_target_exposure(df, sig,
                                     risk_per_trade=config.RISK.max_risk_per_trade,
                                     k_stop=config.RISK.k_stop,
                                     max_leverage=config.RISK.max_position_pct)
        legs[inst.name] = run_backtest(df, expo, inst.alpaca)["net_ret"]
    if not legs:
        return pd.Series(dtype=float)
    idx = sorted(set().union(*[s.index for s in legs.values()]))
    return pd.DataFrame(legs).reindex(idx).fillna(0.0).mean(axis=1)


def validate_trend() -> dict:
    combos = [dict(fast=f, slow=s) for f in TREND_GRID["fast"]
              for s in TREND_GRID["slow"] if f < s]
    is_sharpes, results = [], []
    for params in combos:
        net = _portfolio_net(params)
        if len(net) < 252:
            continue
        k = int(len(net) * SPLIT)
        is_s = M.periodic_sharpe(net.iloc[:k])
        oos_s = M.periodic_sharpe(net.iloc[k:])
        is_sharpes.append(is_s)
        results.append({"params": params, "net": net, "k": k,
                        "is": is_s, "oos": oos_s})

    best = max(results, key=lambda r: r["is"])
    dsr = M.deflated_sharpe_ratio(best["net"].iloc[:best["k"]], is_sharpes)
    oos_ann = best["oos"] * np.sqrt(252)
    passing = bool(dsr["dsr"] is not None and dsr["dsr"] >= config.VALIDATION_MIN_DSR
                   and oos_ann > 0)
    return {
        "strategy": "trend",
        "universe": [i.name for i in config.UNIVERSE],
        "n_trials": len(results),
        "winner_params": best["params"],
        "is_sharpe": round(best["is"] * np.sqrt(252), 3),
        "oos_sharpe": round(oos_ann, 3),
        "deflated_sharpe": round(dsr["dsr"], 4) if dsr["dsr"] is not None else None,
        "passing": passing,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def main():
    if config.STRATEGY != "trend":
        print(f"Automated validation currently supports 'trend' only; "
              f"strategy is '{config.STRATEGY}'. Live gate stays shut.")
        return
    rep = validate_trend()
    path = f"{config.VALIDATION_DIR}/{rep['strategy']}.json"
    with open(path, "w") as f:
        json.dump(rep, f, indent=2)
    print(json.dumps(rep, indent=2))
    verdict = "PASSES — live gate would open" if rep["passing"] else \
              "FAILS — live trading remains blocked (correct if there's no real edge)"
    print(f"\nVerdict: {verdict}")
    print(f"Report written to {path}")


if __name__ == "__main__":
    main()
