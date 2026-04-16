"""
Cipher Bot v5.1 — Market Cipher Strategy Engine
Kraken Pro Paper Trading Bot
Runs 24/7 on Railway

v5 Changes:
- Switched from 200MA back to 50MA (better suited for 4-hour swing trading)
- RSI threshold loosened from 38 to 45 (catches more real dip opportunities)
- Momentum threshold cut in half (from 0.1% to 0.05% of price)
- 200MA was too conservative for swing trading — 50MA spans ~8 days on 4h chart

v5.1 Bug Fixes:
- FIX 1: Exit checks now use live price, not stale candle high/low
- FIX 2: Signal candle lock — one trade per unique candle timestamp only
- FIX 3: Cooldown removed (was decrementing every 30s tick, not per candle)
"""

import os
import time
import requests
import logging
from collections import deque

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("CipherBot")

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY    = os.environ.get("KRAKEN_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")
PAIR       = os.environ.get("TRADING_PAIR", "XBTUSD")
TIMEFRAME  = int(os.environ.get("TIMEFRAME_MINUTES", "240"))   # 4h candles
CAPITAL    = float(os.environ.get("CAPITAL", "10000"))
RISK_PCT   = float(os.environ.get("RISK_PCT", "2"))
TP_MULTI   = float(os.environ.get("TP_MULTI", "2.5"))
PAPER      = os.environ.get("PAPER_MODE", "true").lower() == "true"

MA_PERIOD      = 50    # v5: back to 50MA — right size for 4h swing trading
CANDLES_NEEDED = MA_PERIOD + 15  # enough history for all indicators

# ── State ─────────────────────────────────────────────────────────────────────
candles             = deque(maxlen=CANDLES_NEEDED)
open_trade          = None
tick_count          = 0
wins                = 0
losses              = 0
total_pnl           = 0.0
total_fees          = 0.0
last_signal_candle  = None   # FIX 2: timestamp of candle that triggered last entry

# ── Kraken API ────────────────────────────────────────────────────────────────
def get_price():
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": PAIR},
            timeout=10
        )
        data = r.json()
        if data.get("error"):
            return None
        result = data["result"]
        key = list(result.keys())[0]
        return float(result[key]["c"][0])
    except Exception as e:
        log.error(f"Price fetch error: {e}")
        return None

def get_candles():
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": PAIR, "interval": TIMEFRAME},
            timeout=15
        )
        data = r.json()
        if data.get("error"):
            return []
        result = data["result"]
        key = [k for k in result.keys() if k != "last"][0]
        raw = result[key]
        parsed = []
        for c in raw[:-1]:  # exclude incomplete current candle
            parsed.append({
                "time":   int(c[0]),
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[6])
            })
        return parsed[-CANDLES_NEEDED:]
    except Exception as e:
        log.error(f"Candle fetch error: {e}")
        return []

# ── Indicators ────────────────────────────────────────────────────────────────
def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses_list = [], []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-(i+1)]
        (gains if diff > 0 else losses_list).append(abs(diff))
    avg_gain = sum(gains) / period if gains else 0.0001
    avg_loss = sum(losses_list) / period if losses_list else 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_momentum(closes, period=5):
    if len(closes) < period + 1:
        return 0.0
    return closes[-1] - closes[-(period+1)]

def compute_vwap(candle_list):
    num = sum((c["high"] + c["low"] + c["close"]) / 3 * c["volume"] for c in candle_list)
    den = sum(c["volume"] for c in candle_list)
    return num / den if den else candle_list[-1]["close"]

def compute_ma(closes, period):
    if len(closes) < period:
        return closes[-1]
    return sum(closes[-period:]) / period

# ── Signal Logic ──────────────────────────────────────────────────────────────
def check_signals(candle_list):
    """
    v5 Signal Logic:
    - 50MA trend filter (price must be above 50MA — uptrend confirmed)
    - RSI below 45 (meaningful pullback, not just any dip)
    - Momentum threshold 0.05% of price (half of v4 — catches smaller bounces)
    - Momentum must flip from negative to positive (bottom confirmation)
    - VWAP above required
    """
    if len(candle_list) < CANDLES_NEEDED:
        return None, {}

    closes = [c["close"] for c in candle_list]
    price  = closes[-1]

    rsi       = compute_rsi(closes)
    mom5      = compute_momentum(closes, 5)
    mom10     = compute_momentum(closes, 10)
    prev_mom5 = compute_momentum(closes[:-1], 5) if len(closes) > 6 else 0
    vwap      = compute_vwap(candle_list[-10:])
    ma50      = compute_ma(closes, MA_PERIOD)

    vwap_above    = price > vwap
    above_50ma    = price > ma50
    trending      = abs(mom10) > (price * 0.001)
    mom_threshold = price * 0.0005

    green_dot = (
        rsi < 45 and
        mom5 > mom_threshold and
        prev_mom5 < 0 and
        above_50ma and
        trending
    )

    blue_tri = (
        rsi > 60 and
        mom5 < -mom_threshold and
        prev_mom5 > 0 and
        trending
    )

    signal = None
    if green_dot and vwap_above:
        signal = "BUY"

    indicators = {
        "rsi":        round(rsi, 1),
        "momentum":   round(mom5, 2),
        "vwap":       round(vwap, 2),
        "ma50":       round(ma50, 2),
        "vwap_above": vwap_above,
        "above_50ma": above_50ma,
        "trending":   trending,
        "green_dot":  green_dot,
        "blue_tri":   blue_tri,
        "oversold":   rsi < 45,
    }

    return signal, indicators

# ── Trade Management ──────────────────────────────────────────────────────────
def open_new_trade(entry, signal_candle, indicators):
    global open_trade, last_signal_candle
    stop = signal_candle["low"] * 0.999
    risk = entry - stop
    if risk <= 0:
        log.warning("Risk <= 0, skipping trade.")
        return
    size = (CAPITAL * RISK_PCT / 100) / risk
    tp   = entry + (risk * TP_MULTI)
    fee  = entry * size * 0.001
    open_trade = {
        "entry": entry, "stop": stop, "tp": tp,
        "size": size, "fee_in": fee
    }
    # FIX 2: Record which candle triggered this entry so we never re-enter on same candle
    last_signal_candle = signal_candle["time"]
    mode = "[PAPER]" if PAPER else "[LIVE]"
    log.info(
        f"{mode} TRADE OPENED | Entry: ${entry:,.2f} | Stop: ${stop:,.2f} | "
        f"TP: ${tp:,.2f} | Size: {size:.6f} BTC | Fee: ${fee:.2f}"
    )

def check_trade_exit(price):
    # FIX 1: Use live price (passed in) instead of stale candle high/low
    global open_trade, wins, losses, total_pnl, total_fees
    if not open_trade:
        return

    fee_out   = price * open_trade["size"] * 0.001
    total_fee = open_trade["fee_in"] + fee_out
    mode      = "[PAPER]" if PAPER else "[LIVE]"

    if price <= open_trade["stop"]:
        pnl = (open_trade["stop"] - open_trade["entry"]) * open_trade["size"] - total_fee
        losses    += 1
        total_pnl += pnl
        total_fees += total_fee
        log.info(f"{mode} STOP HIT ❌ | Exit: ${price:,.2f} | Net P&L: ${pnl:,.2f}")
        open_trade = None

    elif price >= open_trade["tp"]:
        pnl = (open_trade["tp"] - open_trade["entry"]) * open_trade["size"] - total_fee
        wins      += 1
        total_pnl += pnl
        total_fees += total_fee
        log.info(f"{mode} TP HIT ✅ | Exit: ${price:,.2f} | Net P&L: ${pnl:,.2f}")
        open_trade = None

# ── Status Printer ─────────────────────────────────────────────────────────────
def print_status(price, indicators):
    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades else 0
    mode = "PAPER" if PAPER else "LIVE"
    log.info(
        f"[{mode}] BTC: ${price:,.2f} | 50MA: ${indicators.get('ma50', 0):,.2f} | "
        f"RSI: {indicators.get('rsi', 0)} | "
        f"Oversold(<45): {indicators.get('oversold', False)} | "
        f"Above50MA: {indicators.get('above_50ma', False)} | "
        f"VWAP↑: {indicators.get('vwap_above', False)} | "
        f"GreenDot: {indicators.get('green_dot', False)}"
    )
    log.info(
        f"Trades: {total_trades} | Wins: {wins} | Losses: {losses} | "
        f"Win Rate: {win_rate:.1f}% | P&L: ${total_pnl:,.2f} | Fees: ${total_fees:.2f}"
    )
    if open_trade:
        log.info(
            f"OPEN TRADE | Entry: ${open_trade['entry']:,.2f} | "
            f"Stop: ${open_trade['stop']:,.2f} | TP: ${open_trade['tp']:,.2f}"
        )

# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    global tick_count
    log.info("=" * 60)
    log.info("Cipher Bot v5.1 — Starting up")
    log.info(f"Pair: {PAIR} | Timeframe: {TIMEFRAME}min | Mode: {'PAPER' if PAPER else 'LIVE'}")
    log.info(f"MA Filter: 50MA | RSI Threshold: <45 | Momentum: 0.05% of price")
    log.info(f"Capital: ${CAPITAL:,.2f} | Risk: {RISK_PCT}% | TP: {TP_MULTI}x")
    log.info("=" * 60)

    while True:
        try:
            # Reload candles every 10 ticks (~5 min), price every tick
            if tick_count % 10 == 0:
                fresh = get_candles()
                if fresh:
                    candles.clear()
                    candles.extend(fresh)

            price = get_price()
            if price and len(candles) >= CANDLES_NEEDED:
                signal, indicators = check_signals(list(candles))

                # FIX 1: Pass live price to exit checker (not stale candle data)
                if open_trade:
                    check_trade_exit(price)

                # FIX 2: Only enter if no open trade AND this candle hasn't fired yet
                if signal == "BUY" and not open_trade:
                    signal_candle = candles[-2] if len(candles) > 1 else candles[-1]
                    if signal_candle["time"] != last_signal_candle:
                        open_new_trade(
                            entry=price,
                            signal_candle=signal_candle,
                            indicators=indicators
                        )
                    else:
                        log.info("Signal on same candle as last trade — skipping re-entry.")

                if tick_count % 10 == 0:
                    print_status(price, indicators)

            tick_count += 1
            time.sleep(30)

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
