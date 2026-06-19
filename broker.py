"""
broker.py  (hardened)
=====================
A thin broker abstraction with two implementations:

  * AlpacaBroker -- talks to Alpaca via the official `alpaca-py` SDK. Paper mode
    is the default (paper=True). The older `alpaca-trade-api` package is deprecated.
  * MockBroker    -- fully in-memory broker for tests (zero credentials/risk).

HARDENING: `alpaca-py` does not expose a request timeout, so a slow/`504` Alpaca
endpoint would otherwise hang forever. Every network call below runs under a hard
wall-clock timeout (SIGALRM). On a timeout we FAIL FAST. Crucially, a timed-out
ORDER is NOT auto-resent -- per Alpaca's own guidance a timed-out order may still
have reached the market, so we mark it ambiguous and leave it to a human to verify
on the dashboard rather than risk a duplicate.

The bot only ever talks to this interface; trading decisions are made upstream by
deterministic rules.
"""

from __future__ import annotations
import signal
from dataclasses import dataclass

REQUEST_TIMEOUT_S = 12.0
_HAS_ALARM = hasattr(signal, "SIGALRM")


class BrokerTimeout(Exception):
    pass


def _run(fn, seconds: float = REQUEST_TIMEOUT_S):
    """Run fn() but abort with BrokerTimeout if it exceeds `seconds` (Unix/main thread)."""
    if not _HAS_ALARM:
        return fn()

    def _handler(signum, frame):
        raise BrokerTimeout(f"Alpaca request exceeded {seconds:.0f}s")

    old = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


@dataclass
class Position:
    symbol: str
    qty: float            # signed: + long, - short
    market_value: float


class BrokerBase:
    def get_equity(self) -> float: ...
    def get_buying_power(self) -> float: ...
    def get_positions(self) -> dict[str, Position]: ...
    def is_market_open(self) -> bool: ...
    def submit_market_order(self, symbol: str, qty: float, side: str) -> dict: ...
    def close_position(self, symbol: str) -> dict: ...
    def close_all(self) -> None: ...
    def cancel_all_orders(self) -> None: ...


# ---------------------------------------------------------------------------
# Real broker: Alpaca via alpaca-py (all calls time-bounded)
# ---------------------------------------------------------------------------
class AlpacaBroker(BrokerBase):
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        from alpaca.trading.client import TradingClient
        if not api_key or not secret_key:
            raise RuntimeError("Alpaca API key/secret missing (set env vars).")
        self.paper = paper
        self.client = TradingClient(api_key, secret_key, paper=paper)

    def get_equity(self) -> float:
        return float(_run(lambda: self.client.get_account()).equity)

    def get_buying_power(self) -> float:
        return float(_run(lambda: self.client.get_account()).buying_power)

    def get_positions(self) -> dict[str, Position]:
        out = {}
        for p in _run(lambda: self.client.get_all_positions()):
            out[p.symbol] = Position(p.symbol, float(p.qty), float(p.market_value))
        return out

    def is_market_open(self) -> bool:
        return bool(_run(lambda: self.client.get_clock()).is_open)

    def submit_market_order(self, symbol: str, qty: float, side: str) -> dict:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        s = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        is_crypto = "/" in symbol
        # Crypto: Alpaca requires GTC/IOC (DAY is rejected) and supports fractional qty.
        # Equities: DAY, with a whole-share fallback if the asset is not fractionable.
        tif = TimeInForce.GTC if is_crypto else TimeInForce.DAY
        q = round(abs(qty), 3)
        if q <= 0:
            return {"symbol": symbol, "skipped": True, "placed": False}

        def _send(quantity):
            req = MarketOrderRequest(symbol=symbol, qty=quantity, side=s,
                                     time_in_force=tif)
            return self.client.submit_order(order_data=req)

        try:
            o = _run(lambda: _send(q))
        except BrokerTimeout:
            # Do NOT resend: the order may already be live at Alpaca.
            return {"symbol": symbol, "side": side, "qty": q, "placed": False,
                    "ambiguous": True,
                    "error": "timeout -- order status UNKNOWN; verify on the Alpaca "
                             "dashboard before resending (it may have reached the market)"}
        except Exception as e:
            # Non-timeout error. Crypto is fractional, so int()-rounding it to a whole
            # unit would zero a sub-1 order -- don't retry, just report. Equities may be
            # non-fractionable, so try one whole-share order.
            if is_crypto or int(q) <= 0:
                return {"symbol": symbol, "side": side, "qty": q, "placed": False,
                        "error": f"{e}"}
            try:
                o = _run(lambda: _send(int(q)))
            except BrokerTimeout:
                return {"symbol": symbol, "side": side, "qty": q, "placed": False,
                        "ambiguous": True, "error": "timeout on retry; verify on dashboard"}
            except Exception as e2:
                return {"symbol": symbol, "side": side, "qty": q, "placed": False,
                        "error": f"{e2}"}
        return {"id": str(getattr(o, "id", "")), "symbol": symbol, "qty": q,
                "side": side, "placed": True}

    def close_position(self, symbol: str) -> dict:
        try:
            _run(lambda: self.client.close_position(symbol))
        except Exception as e:
            return {"symbol": symbol, "error": str(e)}
        return {"symbol": symbol, "closed": True}

    def close_all(self) -> None:
        _run(lambda: self.client.close_all_positions(cancel_orders=True))

    def cancel_all_orders(self) -> None:
        _run(lambda: self.client.cancel_orders())


# ---------------------------------------------------------------------------
# Mock broker: deterministic, in-memory, for tests and dry runs
# ---------------------------------------------------------------------------
class MockBroker(BrokerBase):
    def __init__(self, equity: float = 100_000.0, prices: dict[str, float] | None = None,
                 market_open: bool = True):
        self.cash = equity
        self._equity = equity
        self.prices = prices or {}
        self.market_open = market_open
        self.positions: dict[str, Position] = {}
        self.order_log: list[dict] = []

    def set_price(self, symbol: str, price: float):
        self.prices[symbol] = price

    def mark_to_market(self):
        pos_val = sum(p.qty * self.prices.get(p.symbol, 0.0) for p in self.positions.values())
        self._equity = self.cash + pos_val

    def get_equity(self) -> float:
        self.mark_to_market()
        return self._equity

    def get_buying_power(self) -> float:
        return max(0.0, self.get_equity()) * 2.0

    def get_positions(self) -> dict[str, Position]:
        for p in self.positions.values():
            p.market_value = p.qty * self.prices.get(p.symbol, 0.0)
        return {k: v for k, v in self.positions.items() if abs(v.qty) > 1e-9}

    def is_market_open(self) -> bool:
        return self.market_open

    def submit_market_order(self, symbol: str, qty: float, side: str) -> dict:
        price = self.prices.get(symbol, 0.0)
        signed = abs(qty) if side.lower() == "buy" else -abs(qty)
        self.cash -= signed * price
        cur = self.positions.get(symbol, Position(symbol, 0.0, 0.0))
        cur.qty += signed
        self.positions[symbol] = cur
        rec = {"symbol": symbol, "qty": round(signed, 4), "side": side,
               "price": price, "placed": True}
        self.order_log.append(rec)
        return rec

    def close_position(self, symbol: str) -> dict:
        p = self.positions.get(symbol)
        if not p:
            return {"symbol": symbol, "skipped": True}
        self.cash += p.qty * self.prices.get(symbol, 0.0)
        self.positions.pop(symbol, None)
        return {"symbol": symbol, "closed": True}

    def close_all(self) -> None:
        for s in list(self.positions.keys()):
            self.close_position(s)

    def cancel_all_orders(self) -> None:
        pass
