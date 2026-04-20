"""
Cipher Bot v5.2 — Dual Timeframe Strategy
Kraken Pro Paper Trading Bot
Runs 24/7 on Railway

v5 Changes:
- Switched from 200MA back to 50MA (better suited for 4-hour swing trading)
- RSI threshold loosened from 38 to 45 (catches more real dip opportunities)
- Momentum threshold cut in half (from 0.1% to 0.05% of price)

v5.1 Bug Fixes:
- FIX 1: Exit checks now use live price, not stale candle high/low
- FIX 2: Signal candle lock — one trade per unique candle timestamp only
- FIX 3: Cooldown removed (was decrementing every 30s tick, not per candle)

v5.2 Changes — Dual Timeframe:
- 4-HOUR candles: trend filter only (50MA, VWAP direction)
- 1-HOUR candles: RSI, momentum flip, and entry timing
- Benefit: catches momentum reversals 4x sooner than pure 4h approach
- Stop loss still based on 1h signal candle low for tighter risk control
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
CAPITAL    = float(os.environ.get("CAPITAL", "10000"))
RISK_PCT   = float(os.environ.get("RISK_PCT", "2"))
TP_MULTI   = float(os.environ.get("TP_MULTI", "2.5"))
PAPER      = os.environ.get("PAPER_MODE", "true").lower() == "true"

# Timeframes
TF_TREND  = 240   # 4-hour candles — trend filter (50MA, VWAP)
TF_ENTRY  = 60    # 1-hour candles — momentum flip and entry signal

MA_PERIOD        = 50   # Applied to 4h candles
TF_TREND_NEEDED  = MA_PERIOD + 15
TF_ENTRY_NEEDED  = 20   # 20 x 1h candles is plenty for RSI + momentum

# ── State ─────────────────────────────────────────────────────────────────────
candles_4h          = deque(maxlen=TF_TREND_NEEDED)
candles_1h          = deque(maxlen=TF_ENTRY_NEEDED)
open_trade          = None
tick_count          = 0
wins                = 0
losses              = 0
total_pnl           = 0.0
total_fees          = 0.0
last_signal_candle  = None  # timestamp of 1h candle that last triggered entry

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

def get_candles(interval, count):
    """Fetch completed candles for a given interval (minutes)."""
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": PAIR, "interval": interval},
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
        return parsed[-count:]
    except Exception as e:
        log.error(f"Candle fetch error ({interval}min): {e}")
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
def check_trend(candle_list_4h):
    """
    4-HOUR TREND FILTER
    Confirms we are in an uptrend before allowing any entry.
    Returns (trend_ok, trend_indicators)
    """
    if len(candle_list_4h) < TF_TREND_NEEDED:
        return False, {}

    closes   = [c["close"] for c in candle_list_4h]
    price    = closes[-1]
    ma50     = compute_ma(closes, MA_PERIOD)
    vwap_4h  = compute_vwap(candle_list_4h[-10:])

    above_50ma  = price > ma50
    vwap_above  = price > vwap_4h

    # Trend is valid only if price is above both 50MA and 4h VWAP
    trend_ok = above_50ma and vwap_above

    return trend_ok, {
        "ma50":       round(ma50, 2),
        "vwap_4h":    round(vwap_4h, 2),
        "above_50ma": above_50ma,
        "vwap_above": vwap_above,
    }

def check_entry(candle_list_1h):
    """
    1-HOUR ENTRY SIGNAL
    Looks for RSI oversold + momentum flip on 1h candles.
    Returns (signal, entry_indicators)
    """
    if len(candle_list_1h) < TF_ENTRY_NEEDED:
        return None, {}

    closes        = [c["close"] for c in candle_list_1h]
    price         = closes[-1]
    rsi           = compute_rsi(closes)
    mom5          = compute_momentum(closes, 5)
    mom10         = compute_momentum(closes, 10)
    prev_mom5     = compute_momentum(closes[:-1], 5) if len(closes) > 6 else 0
    mom_threshold = price * 0.0005   # 0.05% of price

    trending  = abs(mom10) > (price * 0.001)

    # GREEN DOT on 1h: RSI oversold + momentum flipping positive
    green_dot = (
        rsi < 45 and
        mom5 > mom_threshold and
        prev_mom5 < 0 and
        trending
    )

    # BLUE TRIANGLE on 1h: overbought exit warning
    blue_tri = (
        rsi > 60 and
        mom5 < -mom_threshold and
        prev_mom5 > 0 and
        trending
    )

    signal = "BUY" if green_dot else None

    return signal, {
        "rsi":       round(rsi, 1),
        "momentum":  round(mom5, 2),
        "green_dot": green_dot,
        "blue_tri":  blue_tri,
        "oversold":  rsi < 45,
        "trending":  trending,
    }

# ── Trade Management ──────────────────────────────────────────────────────────
def open_new_trade(entry, signal_candle_1h, trend_ind, entry_ind):
    global open_trade, last_signal_candle
    stop = signal_candle_1h["low"] * 0.999
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
    last_signal_candle = signal_candle_1h["time"]
    mode = "[PAPER]" if PAPER else "[LIVE]"
    log.info(
        f"{mode} TRADE OPENED | Entry: ${entry:,.2f} | Stop: ${stop:,.2f} | "
        f"TP: ${tp:,.2f} | Size: {size:.6f} BTC | Fee: ${fee:.2f}"
    )
    log.info(
        f"  Trend (4h): 50MA=${trend_ind['ma50']:,.2f} | Above50MA={trend_ind['above_50ma']} | "
        f"VWAP4h=${trend_ind['vwap_4h']:,.2f}"
    )
    log.info(
        f"  Entry (1h): RSI={entry_ind['rsi']} | Momentum={entry_ind['momentum']:.2f} | "
        f"GreenDot={entry_ind['green_dot']}"
    )

def check_trade_exit(price):
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

# ── Status Printer ────────────────────────────────────────────────────────────
def print_status(price, trend_ind, entry_ind):
    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades else 0
    mode = "PAPER" if PAPER else "LIVE"
    log.info(
        f"[{mode}] BTC: ${price:,.2f} | "
        f"50MA(4h): ${trend_ind.get('ma50', 0):,.2f} | "
        f"Above50MA: {trend_ind.get('above_50ma', False)} | "
        f"VWAP4h↑: {trend_ind.get('vwap_above', False)}"
    )
    log.info(
        f"[{mode}] RSI(1h): {entry_ind.get('rsi', 0)} | "
        f"Oversold(<45): {entry_ind.get('oversold', False)} | "
        f"GreenDot(1h): {entry_ind.get('green_dot', False)}"
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
    log.info("Cipher Bot v5.2 — Dual Timeframe Strategy")
    log.info(f"Pair: {PAIR} | Mode: {'PAPER' if PAPER else 'LIVE'}")
    log.info(f"Trend Filter: 4h 50MA + 4h VWAP")
    log.info(f"Entry Signal: 1h RSI <45 + 1h Momentum Flip")
    log.info(f"Capital: ${CAPITAL:,.2f} | Risk: {RISK_PCT}% | TP: {TP_MULTI}x")
    log.info("=" * 60)

    while True:
        try:
            # Reload candles every 10 ticks (~5 min)
            if tick_count % 10 == 0:
                fresh_4h = get_candles(TF_TREND, TF_TREND_NEEDED)
                if fresh_4h:
                    candles_4h.clear()
                    candles_4h.extend(fresh_4h)

                fresh_1h = get_candles(TF_ENTRY, TF_ENTRY_NEEDED)
                if fresh_1h:
                    candles_1h.clear()
                    candles_1h.extend(fresh_1h)

            price = get_price()

            if (price
                    and len(candles_4h) >= TF_TREND_NEEDED
                    and len(candles_1h) >= TF_ENTRY_NEEDED):

                trend_ok, trend_ind = check_trend(list(candles_4h))
                entry_signal, entry_ind = check_entry(list(candles_1h))

                # Exit check — uses live price
                if open_trade:
                    check_trade_exit(price)

                # Entry — requires BOTH 4h trend AND 1h green dot
                if entry_signal == "BUY" and trend_ok and not open_trade:
                    signal_candle_1h = candles_1h[-2] if len(candles_1h) > 1 else candles_1h[-1]
                    if signal_candle_1h["time"] != last_signal_candle:
                        open_new_trade(
                            entry=price,
                            signal_candle_1h=signal_candle_1h,
                            trend_ind=trend_ind,
                            entry_ind=entry_ind
                        )
                    else:
                        log.info("Signal on same 1h candle as last trade — skipping re-entry.")

                if tick_count % 10 == 0:
                    print_status(price, trend_ind, entry_ind)

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
