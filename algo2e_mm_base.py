# -*- coding: utf-8 -*-
"""
Created on Mon Feb  2 10:26:48 2026

@author: marcu_szw8w5w
"""

import requests
import time
from typing import Dict, Tuple, Optional, List

# =========================
# CONFIG
# =========================
API_KEY = "HXRPD5D0"
BASE_URL = "http://localhost:9999/v1"

TICKERS = ["CNR", "RY", "AC"]

# Market-making cadence
LOOP_SLEEP = 0.20          # seconds between loops
REQUOTE_MIN_INTERVAL = 0.50 # don't cancel/replace more often than this per ticker

# Order sizing (safe default; we can increase once stable)
ORDER_QTY = 500

# Limits (case-level)
NET_LIMIT = 25000          # net across all tickers
GROSS_LIMIT = 25000        # gross across all tickers

# Price rounding (most cases are $0.01 tick size)
PRICE_DECIMALS = 2

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
    # Ensure all tickers exist
    for t in TICKERS:
        pos.setdefault(t, 0)
    return pos

def net_and_gross(positions: Dict[str, int]) -> Tuple[int, int]:
    net = sum(positions[t] for t in TICKERS)
    gross = sum(abs(positions[t]) for t in TICKERS)
    return net, gross

def get_open_orders() -> List[dict]:
    """
    RIT commonly supports /orders?status=OPEN
    If it returns wrapper, handle both.
    """
    data = get_json("/orders", params={"status": "OPEN"})
    orders = data["orders"] if isinstance(data, dict) and "orders" in data else data
    # Keep only our tickers
    return [o for o in orders if o.get("ticker") in TICKERS and o.get("status") == "OPEN"]

def cancel_ticker_orders(ticker: str):
    # Most reliable fast cancel in RIT
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
    # resp typically returns an order_id; safe fallback
    return int(resp.get("order_id", -1))


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
# CORE: BASE QUOTING POLICY
# =========================
def can_quote(net: int, gross: int, side: str) -> bool:
    """
    side: "BUY" or "SELL"
    Enforce net + gross constraints.
    """
    if gross >= GROSS_LIMIT:
        return False
    if side == "BUY" and net >= NET_LIMIT:
        return False
    if side == "SELL" and net <= -NET_LIMIT:
        return False
    return True

def need_requote(ticker: str, target_bid: float, target_ask: float) -> bool:
    st = STATE[ticker]
    now = time.time()

    # throttle cancel/replace
    if now - st.last_requote_time < REQUOTE_MIN_INTERVAL:
        return False

    # first time
    if st.bid_px is None or st.ask_px is None:
        return True

    # requote if top-of-book changed enough to matter (>= 1 cent)
    if abs(st.bid_px - target_bid) >= 0.01 or abs(st.ask_px - target_ask) >= 0.01:
        return True

    return False

def requote_ticker(ticker: str, best_bid: float, best_ask: float, net: int, gross: int):
    """
    Base strategy: quote at best bid and best ask (join).
    One bid + one ask per ticker, managed by cancel-and-replace on change.
    """
    target_bid = round(best_bid, PRICE_DECIMALS)
    target_ask = round(best_ask, PRICE_DECIMALS)

    if not need_requote(ticker, target_bid, target_ask):
        return

    # Cancel existing orders for this ticker
    cancel_ticker_orders(ticker)

    # Place new quotes if allowed by limits
    if can_quote(net, gross, "BUY"):
        place_limit(ticker, "BUY", ORDER_QTY, target_bid)

    if can_quote(net, gross, "SELL"):
        place_limit(ticker, "SELL", ORDER_QTY, target_ask)

    # Update state
    st = STATE[ticker]
    st.last_requote_time = time.time()
    st.bid_px = target_bid
    st.ask_px = target_ask


# =========================
# MAIN LOOP
# =========================
def main():
    print("Starting ALGO2e base market maker...")
    print("BASE_URL:", BASE_URL)
    print("TICKERS:", TICKERS)

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
            net, gross = net_and_gross(positions)

            # Log heartbeat (concise but informative)
            print(f"tick={tick} net={net} gross={gross} pos={positions}")

            for t in TICKERS:
                bbo = get_book(t)
                if bbo is None:
                    continue
                bb, ba = bbo
                requote_ticker(t, bb, ba, net, gross)

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
