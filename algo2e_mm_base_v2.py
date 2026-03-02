# -*- coding: utf-8 -*-
"""
Created on Mon Feb  2 11:07:14 2026

@author: marcu_szw8w5w
"""

# -*- coding: utf-8 -*-
"""
ALGO2e Market Maker (updated)

Changes:
1) Slow down cancel/replace in quarter-spread mode (more throttling + larger price-change threshold)
2) Limit checks include OPEN orders and "projected" exposure (positions + open orders + proposed new orders)

"""

import requests
import time
from typing import Dict, Tuple, Optional, List, Iterable

# =========================
# CONFIG
# =========================
API_KEY = "HXRPD5D0"
BASE_URL = "http://localhost:9999/v1"

TICKERS = ["CNR", "RY", "AC"]

# Quoting mode: "join" (old behavior) or "quarter"
QUOTE_MODE = "quarter"   # <-- change to "join" if you want the original join-BBO behavior

# Market-making cadence
LOOP_SLEEP = 0.20  # seconds between loops

# Cancel/replace throttles (global per-ticker)
REQUOTE_MIN_INTERVAL_JOIN = 0.50
REQUOTE_MIN_INTERVAL_QUARTER = 1.25   # Change #1: slower cancel/replace in quarter spread

# Price movement threshold that triggers a requote
REQUOTE_PX_THRESHOLD_JOIN = 0.01
REQUOTE_PX_THRESHOLD_QUARTER = 0.02   # Change #1: be less twitchy in quarter spread

# Order sizing
ORDER_QTY = 500

# Limits (case-level)
NET_LIMIT = 25000
GROSS_LIMIT = 25000

# Price rounding
PRICE_DECIMALS = 2
TICK_SIZE = 0.01

# =========================
# SESSION SETUP
# =========================
s = requests.Session()
s.headers.update({"X-API-Key": API_KEY})


# =========================
# HELPERS: HTTP
# =========================
def get_json(path: str, params: dict = None):
    r = s.get(f"{BASE_URL}{path}", params=params, timeout=2)
    if not r.ok:
        raise RuntimeError(f"GET {path} failed {r.status_code}: {r.text[:200]}")
    return r.json()

def post_json(path: str, params: dict = None):
    r = s.post(f"{BASE_URL}{path}", params=params, timeout=2)
    if not r.ok:
        raise RuntimeError(f"POST {path} failed {r.status_code}: {r.text[:200]}")
    return r.json()


# =========================
# HELPERS: RIT DATA
# =========================
def get_case() -> Tuple[str, int]:
    data = get_json("/case")
    return data.get("status", ""), int(data.get("tick", 0))

def get_book(ticker: str) -> Optional[Tuple[float, float]]:
    book = get_json("/securities/book", params={"ticker": ticker})
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return None
    best_bid = float(bids[0]["price"])
    best_ask = float(asks[0]["price"])
    return best_bid, best_ask

def get_positions() -> Dict[str, int]:
    """
    Returns {ticker: position}. RIT sometimes returns a list directly or a dict wrapper.
    """
    data = get_json("/securities")
    secs = data["securities"] if isinstance(data, dict) and "securities" in data else data

    pos = {}
    for sec in secs:
        t = sec.get("ticker")
        if t in TICKERS:
            pos[t] = int(sec.get("position", 0))
    for t in TICKERS:
        pos.setdefault(t, 0)
    return pos

def get_open_orders() -> List[dict]:
    """
    Pull OPEN orders and keep only our tickers.
    """
    data = get_json("/orders", params={"status": "OPEN"})
    orders = data["orders"] if isinstance(data, dict) and "orders" in data else data
    return [o for o in orders if o.get("ticker") in TICKERS and o.get("status") == "OPEN"]

def cancel_ticker_orders(ticker: str):
    post_json("/commands/cancel", params={"ticker": ticker})

def place_limit(ticker: str, action: str, qty: int, price: float) -> int:
    params = {
        "ticker": ticker,
        "type": "LIMIT",
        "quantity": qty,
        "price": round(price, PRICE_DECIMALS),
        "action": action
    }
    resp = post_json("/orders", params=params)
    return int(resp.get("order_id", -1))


# =========================
# EXPOSURE / LIMITS (positions + open orders + projected)
# =========================
def summarize_open_orders(open_orders: List[dict]) -> Dict[str, Dict[str, int]]:
    """
    Returns:
      { ticker: {"BUY": buy_qty_open, "SELL": sell_qty_open} }
    """
    out = {t: {"BUY": 0, "SELL": 0} for t in TICKERS}
    for o in open_orders:
        t = o.get("ticker")
        if t not in out:
            continue
        side = str(o.get("action", "")).upper()
        qty = int(o.get("quantity", 0))
        if side in ("BUY", "SELL"):
            out[t][side] += qty
    return out

def projected_positions(
    positions: Dict[str, int],
    open_summary: Dict[str, Dict[str, int]],
    exclude_ticker: Optional[str] = None,
    add_orders: Iterable[Tuple[str, str, int]] = (),
) -> Dict[str, int]:
    """
    "Projected" positions assuming all OPEN orders fill, plus any additional orders we plan to add.

    exclude_ticker: if set, we ignore currently open orders for that ticker
                    (useful when we're about to cancel/replace that ticker).
    add_orders: iterable of (ticker, side, qty) for the *new* quotes we plan to add.
    """
    proj = {t: int(positions.get(t, 0)) for t in TICKERS}

    # existing open orders
    for t in TICKERS:
        if exclude_ticker is not None and t == exclude_ticker:
            continue
        buy_q = int(open_summary.get(t, {}).get("BUY", 0))
        sell_q = int(open_summary.get(t, {}).get("SELL", 0))
        proj[t] += buy_q
        proj[t] -= sell_q

    # proposed new orders
    for t, side, qty in add_orders:
        if t not in proj:
            continue
        sside = side.upper()
        if sside == "BUY":
            proj[t] += qty
        elif sside == "SELL":
            proj[t] -= qty

    return proj

def net_and_gross_from_positions(pos: Dict[str, int]) -> Tuple[int, int]:
    net = sum(pos[t] for t in TICKERS)
    gross = sum(abs(pos[t]) for t in TICKERS)
    return net, gross

def can_add_order_with_limits(
    positions: Dict[str, int],
    open_summary: Dict[str, Dict[str, int]],
    ticker_to_requote: Optional[str],
    side: str,
    qty: int,
    other_new_orders: Iterable[Tuple[str, str, int]] = (),
) -> bool:
    """
    Change #2: Limits must include OPEN orders and projected exposure.
    We check limits *as if* open orders fill and our new order(s) fill too.

    ticker_to_requote: if we're cancel/replacing this ticker, we exclude its existing open orders
                       (because we're about to nuke them) to avoid double-counting.
    other_new_orders: any other new orders we already intend to place alongside this one.
    """
    proposed = list(other_new_orders) + [(ticker_to_requote or "", side, qty)]
    # Replace blank ticker with the actual ticker_to_requote in the tuple we appended
    proposed = [
        (ticker_to_requote if t == "" else t, s, q)
        for (t, s, q) in proposed
    ]

    proj = projected_positions(
        positions=positions,
        open_summary=open_summary,
        exclude_ticker=ticker_to_requote,
        add_orders=proposed,
    )
    net, gross = net_and_gross_from_positions(proj)

    if gross > GROSS_LIMIT:
        return False
    if net > NET_LIMIT:
        return False
    if net < -NET_LIMIT:
        return False
    return True


# =========================
# QUOTING STATE
# =========================
class QuoteState:
    def __init__(self):
        self.last_requote_time = 0.0
        self.bid_px = None
        self.ask_px = None

STATE = {t: QuoteState() for t in TICKERS}


# =========================
# PRICE / QUOTE LOGIC
# =========================
def round_to_tick(px: float) -> float:
    return round(round(px / TICK_SIZE) * TICK_SIZE, PRICE_DECIMALS)

def compute_targets(ticker: str, best_bid: float, best_ask: float) -> Tuple[float, float]:
    """
    QUOTE_MODE:
      - join: bid=best_bid, ask=best_ask
      - quarter: move 1/4 of the spread inward on both sides
                 bid = best_bid + 0.25*spread
                 ask = best_ask - 0.25*spread
    """
    bb = float(best_bid)
    ba = float(best_ask)
    if ba <= bb:
        # crossed/locked book, fall back to join
        return round_to_tick(bb), round_to_tick(ba)

    if QUOTE_MODE.lower() == "quarter":
        spr = ba - bb
        # move inside by 1/4 spread
        tb = bb + 0.25 * spr
        ta = ba - 0.25 * spr

        # safety: do not cross
        if tb >= ta:
            # if spread tiny, just join
            tb, ta = bb, ba
        return round_to_tick(tb), round_to_tick(ta)

    # default join
    return round_to_tick(bb), round_to_tick(ba)

def requote_params() -> Tuple[float, float]:
    """
    Returns (min_interval, price_threshold) depending on mode.
    """
    if QUOTE_MODE.lower() == "quarter":
        return REQUOTE_MIN_INTERVAL_QUARTER, REQUOTE_PX_THRESHOLD_QUARTER
    return REQUOTE_MIN_INTERVAL_JOIN, REQUOTE_PX_THRESHOLD_JOIN

def need_requote(ticker: str, target_bid: float, target_ask: float) -> bool:
    st = STATE[ticker]
    now = time.time()
    min_interval, px_thresh = requote_params()

    # Change #1: throttle cancel/replace more in quarter mode
    if now - st.last_requote_time < min_interval:
        return False

    if st.bid_px is None or st.ask_px is None:
        return True

    if abs(st.bid_px - target_bid) >= px_thresh or abs(st.ask_px - target_ask) >= px_thresh:
        return True

    return False

def requote_ticker(
    ticker: str,
    best_bid: float,
    best_ask: float,
    positions: Dict[str, int],
    open_summary: Dict[str, Dict[str, int]],
):
    """
    One bid + one ask per ticker. Cancel-and-replace when target changes enough and throttle permits.
    Uses projected exposure (positions + open orders + proposed new quotes) for limit checks.
    """
    target_bid, target_ask = compute_targets(ticker, best_bid, best_ask)

    if not need_requote(ticker, target_bid, target_ask):
        return

    # Cancel existing orders for this ticker (we are replacing them)
    cancel_ticker_orders(ticker)

    # Decide whether each side can be placed using projected exposure checks
    new_orders: List[Tuple[str, str, int]] = []

    # Try BUY first
    if can_add_order_with_limits(
        positions=positions,
        open_summary=open_summary,
        ticker_to_requote=ticker,
        side="BUY",
        qty=ORDER_QTY,
        other_new_orders=new_orders,
    ):
        place_limit(ticker, "BUY", ORDER_QTY, target_bid)
        new_orders.append((ticker, "BUY", ORDER_QTY))

    # Then SELL, considering that BUY might already be added
    if can_add_order_with_limits(
        positions=positions,
        open_summary=open_summary,
        ticker_to_requote=ticker,
        side="SELL",
        qty=ORDER_QTY,
        other_new_orders=new_orders,
    ):
        place_limit(ticker, "SELL", ORDER_QTY, target_ask)
        new_orders.append((ticker, "SELL", ORDER_QTY))

    # Update state
    st = STATE[ticker]
    st.last_requote_time = time.time()
    st.bid_px = target_bid
    st.ask_px = target_ask


# =========================
# MAIN LOOP
# =========================
def main():
    print("Starting ALGO2e market maker (updated)...")
    print("BASE_URL:", BASE_URL)
    print("TICKERS:", TICKERS)
    print("QUOTE_MODE:", QUOTE_MODE)

    status, tick = get_case()
    print(f"Connected. status={status}, tick={tick}")

    # Clean start: cancel all tickers once
    for t in TICKERS:
        try:
            cancel_ticker_orders(t)
        except Exception as e:
            print("Cancel error on start:", t, repr(e))

    while True:
        try:
            status, tick = get_case()
            if status.upper() not in ["ACTIVE", "RUNNING"]:
                time.sleep(0.5)
                continue

            positions = get_positions()
            open_orders = get_open_orders()
            open_summary = summarize_open_orders(open_orders)

            # "Projected if all OPEN fill" view (for logging / sanity)
            proj = projected_positions(positions, open_summary, exclude_ticker=None, add_orders=())
            net_proj, gross_proj = net_and_gross_from_positions(proj)

            # Log heartbeat (includes projected exposure)
            print(
                f"tick={tick} "
                f"pos={positions} "
                f"open={ {t: open_summary[t] for t in TICKERS} } "
                f"proj_pos={proj} net_proj={net_proj} gross_proj={gross_proj}"
            )

            for t in TICKERS:
                bbo = get_book(t)
                if bbo is None:
                    continue
                bb, ba = bbo
                requote_ticker(t, bb, ba, positions, open_summary)

            time.sleep(LOOP_SLEEP)

        except KeyboardInterrupt:
            print("\nStopping. Cancelling all orders...")
            for t in TICKERS:
                try:
                    cancel_ticker_orders(t)
                except:
                    pass
            break

        except Exception as e:
            print("ERROR:", repr(e))
            time.sleep(0.5)

if __name__ == "__main__":
    main()
