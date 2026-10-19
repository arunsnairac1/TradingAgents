"""Zerodha equity-intraday (NSE) charges.

Rates are the published schedule at the time of writing; they change, so verify
against zerodha.com/charges and override here if they differ.
"""

BROKERAGE_PCT = 0.0003       # 0.03% of turnover ...
BROKERAGE_CAP = 20.0         # ... or Rs 20 per executed order, whichever is lower
STT_SELL_PCT = 0.00025       # sell side only
EXCHANGE_TXN_PCT = 0.0000297  # NSE, both sides
SEBI_PCT = 0.000001          # Rs 10 per crore
STAMP_BUY_PCT = 0.00003      # buy side only
GST_PCT = 0.18               # on brokerage + exchange txn + SEBI


def order_charges(side: str, qty: int, price: float) -> float:
    """Total charges (rupees) for one executed order."""
    turnover = qty * price
    brokerage = min(BROKERAGE_CAP, turnover * BROKERAGE_PCT)
    txn = turnover * EXCHANGE_TXN_PCT
    sebi = turnover * SEBI_PCT
    gst = (brokerage + txn + sebi) * GST_PCT
    stt = turnover * STT_SELL_PCT if side.upper() == "SELL" else 0.0
    stamp = turnover * STAMP_BUY_PCT if side.upper() == "BUY" else 0.0
    return brokerage + txn + sebi + gst + stt + stamp


def round_trip_charges(direction: str, qty: int, entry: float, exit_: float) -> float:
    """Charges for opening and closing a position; direction is LONG or SHORT."""
    open_side, close_side = ("BUY", "SELL") if direction == "LONG" else ("SELL", "BUY")
    return order_charges(open_side, qty, entry) + order_charges(close_side, qty, exit_)


def net_pnl(direction: str, qty: int, entry: float, exit_: float) -> float:
    gross = (exit_ - entry) * qty if direction == "LONG" else (entry - exit_) * qty
    return gross - round_trip_charges(direction, qty, entry, exit_)
