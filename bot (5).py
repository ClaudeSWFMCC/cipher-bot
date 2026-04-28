"""
Cipher Bot v5.4 — Market Cipher Strategy Engine
Kraken Pro Paper Trading Bot
Runs 24/7 on Railway

Change from v5.3:
  VWAP filter switched from 4h to 1h timeframe.
  The 4h VWAP lagged so far behind price that it was always pointing
  down when RSI was oversold — the two conditions were structurally
  blocking each other. The 1h VWAP responds fast enough to flip
  upward at the same time a genuine bounce begins.
"""

import os
import time
import hmac
import hashlib
import base64
import urllib.parse
import requests
import json
import logging
from datetime import datetime
from collections import deque

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("CipherBot")

# ── Config from environment variables ─────────────────────────────────────────
API_KEY    = os.environ.get("KRAKEN_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")
PAIR       = os.environ.get("TRADING_PAIR", "XBTUSD")
TIMEFRAME  = int(os.environ.get("TIMEFRAME_MINUTES", "60"))    # 1h candles for entry signal
CAPITAL    = float(os.environ.get("CAPITAL", "17500"))
RISK_PCT   = float(os.environ.get("RISK_PCT", "2"))
TP_MULTI   = float(os.environ.get("TP_MULTI", "2.5"))
PAPER_MODE = os.environ.get("PAPER_MODE", "true").lower() == "true"
FEE_RATE   = 0.001   # 0.10% Kraken Pro

# ── Timeframes ─────────────────────────────────────────────────────────────────
TF_1H  = 60     # 1h candles  — entry signal + VWAP (v5.4 change)
TF_4H  = 240    # 4h candles  — trend filter (50MA only)

MIN_STOP_PCT = 0.015   # 1.5% minimum stop distance
MAX_BTC_SIZE = 0.25    # hard cap per trade

# ── Kraken REST helpers ────────────────────────────────────────────────────────
BASE_URL = "https://api.kraken.com"

def kraken_public(endpoint, params=None):
    url = BASE_URL + "/0/public/" + endpoint
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise ValueError(f"Kraken error: {data['error']}")
    return data["result"]

def kraken_private(endpoint, data=None):
    if not API_KEY or not API_SECRET:
        raise ValueError("API keys not configured")
    url_path = "/0/private/" + endpoint
    nonce = str(int(time.time() * 1000))
    post_data = {"nonce": nonce}
    if data:
        post_data.update(data)
    post_str = urllib.parse.urlencode(post_data)
    encoded = (nonce + post_str).encode()
    message = url_path.encode() + hashlib.sha256(encoded).digest()
    secret = base64.b64decode(API_SECRET)
    sig = hmac.new(secret, message, hashlib.sha512)
    sig_b64 = base64.b64encode(sig.digest()).decode()
    headers = {"API-Key": API_KEY, "API-Sign": sig_b64}
    r = requests.post(BASE_URL + url_path, data=post_data, headers=headers, timeout=10)
    r.raise_for_status()
    data_resp = r.json()
    if data_resp.get("error"):
        raise ValueError(f"Kraken error: {data_resp['error']}")
    return data_resp["result"]

# ── Market data ────────────────────────────────────────────────────────────────
def get_candles(pair, interval, count=200):
    result = kraken_public("OHLC", {"pair": pair, "interval": interval})
    key = list(result.keys())[0]
    raw = result[key][-count:]
    candles = []
    for c in raw:
        candles.append({
            "time":   int(c[0]),
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[6]),
        })
    return candles

def get_ticker(pair):
    result = kraken_public("Ticker", {"pair": pair})
    key = list(result.keys())[0]
    return float(result[key]["c"][0])

# ── Indicators ─────────────────────────────────────────────────────────────────
def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        delta = closes[-period + i] - closes[-period + i - 1]
        (gains if delta > 0 else losses).append(abs(delta))
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 1e-10
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_momentum(closes, period=5):
    if len(closes) < period + 1:
        return 0
    return closes[-1] - closes[-period - 1]

def compute_vwap(candles):
    """Typical price * volume / total volume."""
    num = sum(((c["high"] + c["low"] + c["close"]) / 3) * c["volume"] for c in candles)
    den = sum(c["volume"] for c in candles)
    return num / den if den else candles[-1]["close"]

def compute_ma(closes, period=50):
    if len(closes) < period:
        return closes[-1]
    return sum(closes[-period:]) / period

# ── Signal logic ───────────────────────────────────────────────────────────────
def check_signals(candles_1h, candles_4h):
    """
    Entry requires ALL of:
      1. RSI(1h) < 45           — oversold on 1h chart
      2. Green Dot(1h)          — RSI < 38 + momentum flip from negative to positive
      3. VWAP(1h)↑              — price above 1h VWAP  [v5.4: was 4h]
      4. Price above 50MA(4h)   — macro uptrend intact

    Using 1h VWAP instead of 4h VWAP means the VWAP filter responds
    quickly enough to flip positive at the same candle a bounce begins,
    so it no longer structurally blocks trades when RSI is oversold.
    """
    if len(candles_1h) < 50 or len(candles_4h) < 55:
        return None, {}

    closes_1h = [c["close"] for c in candles_1h]
    closes_4h = [c["close"] for c in candles_4h]
    price     = closes_1h[-1]

    # --- 1h indicators ---
    rsi       = compute_rsi(closes_1h)
    mom5      = compute_momentum(closes_1h, 5)
    prev_mom5 = compute_momentum(closes_1h[:-1], 5) if len(closes_1h) > 6 else 0
    trending  = abs(compute_momentum(closes_1h, 10)) > (price * 0.001)

    # VWAP now uses 1h candles (last 20 × 1h bars ≈ same window as before but responsive)
    vwap_1h      = compute_vwap(candles_1h[-20:])
    vwap_1h_up   = price > vwap_1h

    # --- 4h indicators (trend filter only) ---
    ma50_4h      = compute_ma(closes_4h, 50)
    above_50ma   = price > ma50_4h

    # Green Dot: deeply oversold + momentum flip from negative to positive
    green_dot = (
        rsi < 38
        and mom5 > (price * 0.001)
        and prev_mom5 < 0
        and trending
    )

    # Full entry: Green Dot + 1h VWAP up + above 4h 50MA
    signal = None
    if green_dot and vwap_1h_up and above_50ma:
        signal = "BUY"

    indicators = {
        "price":       round(price, 2),
        "rsi":         round(rsi, 1),
        "momentum":    round(mom5, 2),
        "vwap_1h":     round(vwap_1h, 2),
        "vwap_1h_up":  vwap_1h_up,
        "ma50_4h":     round(ma50_4h, 2),
        "above_50ma":  above_50ma,
        "green_dot":   green_dot,
        "oversold":    rsi < 45,
    }
    return signal, indicators

# ── Trade management ───────────────────────────────────────────────────────────
class TradeState:
    def __init__(self):
        self.position    = None   # {"entry": float, "size": float, "stop": float, "target": float}
        self.trades      = 0
        self.wins        = 0
        self.losses      = 0
        self.pnl         = 0.0
        self.total_fees  = 0.0

    def open_trade(self, price, capital):
        risk_amount  = capital * (RISK_PCT / 100)
        stop_dist    = max(price * MIN_STOP_PCT, risk_amount / (capital / price))
        stop_price   = price - stop_dist
        size         = min(risk_amount / stop_dist, MAX_BTC_SIZE)
        target_price = price + (stop_dist * TP_MULTI)
        fee          = size * price * FEE_RATE
        self.position = {
            "entry":  price,
            "size":   size,
            "stop":   stop_price,
            "target": target_price,
        }
        self.total_fees += fee
        log.info(f"[{'PAPER' if PAPER_MODE else 'LIVE'}] BUY  | Entry: ${price:,.2f} | "
                 f"Size: {size:.4f} BTC | Stop: ${stop_price:,.2f} | "
                 f"Target: ${target_price:,.2f} | Fee: ${fee:.2f}")

    def check_exit(self, price):
        if not self.position:
            return
        p = self.position
        hit_stop   = price <= p["stop"]
        hit_target = price >= p["target"]
        if hit_stop or hit_target:
            pnl   = (price - p["entry"]) * p["size"]
            fee   = p["size"] * price * FEE_RATE
            net   = pnl - fee
            self.pnl        += net
            self.total_fees += fee
            self.trades     += 1
            if net > 0:
                self.wins   += 1
                outcome = "WIN "
            else:
                self.losses += 1
                outcome = "LOSS"
            log.info(f"[{'PAPER' if PAPER_MODE else 'LIVE'}] {outcome} | Exit: ${price:,.2f} | "
                     f"P&L: ${net:+.2f} | Reason: {'Stop' if hit_stop else 'Target'}")
            self.position = None

    def win_rate(self):
        return (self.wins / self.trades * 100) if self.trades else 0.0

# ── Main loop ──────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("Cipher Bot v5.4 — 1h VWAP Filter")
    log.info(f"Pair: {PAIR} | Mode: {'PAPER' if PAPER_MODE else 'LIVE'}")
    log.info(f"Trend Filter: 4h 50MA | Entry VWAP: 1h (v5.4)")
    log.info(f"Entry Signal: 1h RSI <45 + Green Dot + 1h VWAP↑ + Above 4h 50MA")
    log.info(f"Capital: ${CAPITAL:,.2f} | Risk: {RISK_PCT}% | TP: {TP_MULTI}x")
    log.info(f"Min Stop: {MIN_STOP_PCT*100}% of price | Max Size: {MAX_BTC_SIZE} BTC")
    log.info("=" * 60)

    state = TradeState()

    while True:
        try:
            candles_1h = get_candles(PAIR, TF_1H, 200)
            candles_4h = get_candles(PAIR, TF_4H, 200)
            price      = candles_1h[-1]["close"]

            signal, ind = check_signals(candles_1h, candles_4h)

            log.info(
                f"[{'PAPER' if PAPER_MODE else 'LIVE'}] "
                f"BTC: ${price:,.2f} | "
                f"50MA(4h): ${ind.get('ma50_4h', 0):,.2f} | "
                f"Above50MA: {ind.get('above_50ma', False)} | "
                f"VWAP(1h)↑: {ind.get('vwap_1h_up', False)}"
            )
            log.info(
                f"[{'PAPER' if PAPER_MODE else 'LIVE'}] "
                f"RSI(1h): {ind.get('rsi', 0)} | "
                f"Oversold(<45): {ind.get('oversold', False)} | "
                f"GreenDot(1h): {ind.get('green_dot', False)} | "
                f"Momentum: {ind.get('momentum', 0):.2f}"
            )

            # Check exits first
            if state.position:
                state.check_exit(price)

            # Check entry
            if signal == "BUY" and not state.position:
                state.open_trade(price, CAPITAL)

            log.info(
                f"Trades: {state.trades} | Wins: {state.wins} | "
                f"Losses: {state.losses} | Win Rate: {state.win_rate():.1f}% | "
                f"P&L: ${state.pnl:.2f} | Fees: ${state.total_fees:.2f}"
            )

        except Exception as e:
            log.error(f"Error: {e}")

        time.sleep(300)   # check every 5 minutes

if __name__ == "__main__":
    main()
