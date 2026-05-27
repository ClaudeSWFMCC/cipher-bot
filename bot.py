"""
Cipher Bot v5.7 — Two-Sided Trading (Long + Short)
Kraken Pro Paper Trading Bot
Runs 24/7 on Railway

v5 Changes:
- Switched from 200MA back to 50MA (better suited for 4-hour swing trading)
- RSI threshold loosened from 38 to 45
- Momentum threshold cut in half (from 0.1% to 0.05% of price)

v5.1 Bug Fixes:
- Exit checks now use live price, not stale candle high/low
- Signal candle lock — one trade per unique candle timestamp only
- Cooldown removed

v5.2 Changes — Dual Timeframe:
- 4-HOUR candles: trend filter only
- 1-HOUR candles: RSI, momentum, entry timing

v5.3 Fixes — Position Size & Fee Control:
- Minimum stop distance 1.5% of entry price
- Maximum position size capped at 0.25 BTC
- Fee as % of risk logged on every trade

v5.4 Changes — Relaxed Entry Conditions:
- Phantom fee bug fixed
- RSI threshold raised to 55
- Momentum flip requirement removed
- VWAP switched back to 4h

v5.5 Changes — Trailing Stop + Higher Trade Frequency:
- Trade counter bug fixed
- Trailing stop L1/L2/L3 added
- Trending check removed
- RSI raised to 60
- VWAP demoted to advisory only

v5.6 Changes — 20MA + Tighter Trailing Stop:
- MA period lowered from 50 to 20
- Trailing stop L1 lowered from 50% to 33%

v5.7 Changes — Two-Sided Trading:
- LONG side: unchanged from v5.6
  - Entry: price above 20MA + RSI < 60 + positive momentum
  - Stop: 1.5% below entry
  - TP: 2.5x risk above entry
- SHORT side: mirror of long
  - Entry: price below 20MA + RSI > 40 + negative momentum
  - Stop: 1.5% above entry
  - TP: 2.5x risk below entry
- SAFETY: only one trade open at a time — long OR short, never both
- Trailing stop applies to both directions
- Signal candle lock applies to both directions
- Direction logged clearly on every trade open and close
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
TF_TREND  = 240   # 4-hour candles — trend filter (20MA)
TF_ENTRY  = 60    # 1-hour candles — RSI and momentum entry signal

MA_PERIOD        = 20
TF_TREND_NEEDED  = MA_PERIOD + 15
TF_ENTRY_NEEDED  = 20

# Position size controls
MIN_STOP_PCT  = 0.015   # Minimum stop distance = 1.5% of entry price
MAX_BTC_SIZE  = 0.25    # Hard cap: never trade more than 0.25 BTC at once

# Trailing stop levels
TRAIL_L1_PCT  = 0.33   # 33% to TP → move stop to breakeven
TRAIL_L2_PCT  = 0.75   # 75% to TP → move stop to entry +/- 1%
TRAIL_L3_PCT  = 0.90   # 90% to TP → trail stop 1.5% from current price

# ── State ─────────────────────────────────────────────────────────────────────
candles_4h          = deque(maxlen=TF_TREND_NEEDED)
candles_1h          = deque(maxlen=TF_ENTRY_NEEDED)
open_trade          = None   # dict with 'direction': 'long' or 'short'
tick_count          = 0
wins                = 0
losses              = 0
total_pnl           = 0.0
total_fees          = 0.0
last_signal_candle  = None

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
        for c in raw[:-1]:
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
    """4-HOUR TREND — 20MA primary, VWAP advisory."""
    if len(candle_list_4h) < TF_TREND_NEEDED:
        return None, {}
    closes     = [c["close"] for c in candle_list_4h]
    price      = closes[-1]
    ma20       = compute_ma(closes, MA_PERIOD)
    vwap_4h    = compute_vwap(candle_list_4h[-10:])
    above_ma   = price > ma20
    below_ma   = price < ma20
    vwap_above = price > vwap_4h

    # Returns 'long', 'short', or None
    if above_ma:
        direction = "long"
    elif below_ma:
        direction = "short"
    else:
        direction = None

    return direction, {
        "ma20":       round(ma20, 2),
        "vwap_4h":    round(vwap_4h, 2),
        "above_ma":   above_ma,
        "below_ma":   below_ma,
        "vwap_above": vwap_above,
    }

def check_entry(candle_list_1h, trend_direction):
    """
    1-HOUR ENTRY SIGNAL — mirrors on both sides.
    LONG:  RSI < 60 + positive momentum
    SHORT: RSI > 40 + negative momentum
    """
    if len(candle_list_1h) < TF_ENTRY_NEEDED:
        return None, {}
    closes        = [c["close"] for c in candle_list_1h]
    price         = closes[-1]
    rsi           = compute_rsi(closes)
    mom5          = compute_momentum(closes, 5)
    mom_threshold = price * 0.0005

    long_signal  = (rsi < 60 and mom5 > mom_threshold)
    short_signal = (rsi > 40 and mom5 < -mom_threshold)

    if trend_direction == "long" and long_signal:
        signal = "BUY"
    elif trend_direction == "short" and short_signal:
        signal = "SELL"
    else:
        signal = None

    return signal, {
        "rsi":          round(rsi, 1),
        "momentum":     round(mom5, 2),
        "long_signal":  long_signal,
        "short_signal": short_signal,
        "signal":       signal,
    }

# ── Trade Management ──────────────────────────────────────────────────────────
def open_new_trade(entry, signal, signal_candle_1h, trend_ind, entry_ind):
    global open_trade, last_signal_candle

    direction = "long" if signal == "BUY" else "short"

    if direction == "long":
        stop = min(signal_candle_1h["low"] * 0.999, entry * (1 - MIN_STOP_PCT))
        tp   = entry + ((entry - stop) * TP_MULTI)
    else:
        stop = max(signal_candle_1h["high"] * 1.001, entry * (1 + MIN_STOP_PCT))
        tp   = entry - ((stop - entry) * TP_MULTI)

    risk = abs(entry - stop)
    if risk <= 0:
        log.warning("Risk <= 0, skipping trade.")
        return

    raw_size     = (CAPITAL * RISK_PCT / 100) / risk
    size         = min(raw_size, MAX_BTC_SIZE)
    fee_in       = entry * size * 0.001
    risk_dollars = CAPITAL * RISK_PCT / 100
    fee_pct_risk = (fee_in / risk_dollars) * 100
    stop_dist    = abs(entry - stop)
    stop_pct     = (stop_dist / entry) * 100

    vwap_warning = "" if trend_ind["vwap_above"] == (direction == "long") else " ⚠️ VWAP against trade"
    mode = "[PAPER]" if PAPER else "[LIVE]"
    dir_emoji = "📈" if direction == "long" else "📉"

    open_trade = {
        "direction":   direction,
        "entry":       entry,
        "stop":        stop,
        "tp":          tp,
        "size":        size,
        "fee_in":      fee_in,
        "trail_level": 0
    }
    last_signal_candle = signal_candle_1h["time"]

    log.info(
        f"{mode} {dir_emoji} {direction.upper()} OPENED | Entry: ${entry:,.2f} | "
        f"Stop: ${stop:,.2f} | TP: ${tp:,.2f} | "
        f"Size: {size:.6f} BTC | Fee-in: ${fee_in:.2f}{vwap_warning}"
    )
    log.info(
        f"  Stop dist: ${stop_dist:,.2f} ({stop_pct:.2f}%) | "
        f"Raw size: {raw_size:.4f} → Capped: {size:.4f} BTC | "
        f"Fee as % of risk: {fee_pct_risk:.1f}%"
    )
    log.info(
        f"  Trend (4h): 20MA=${trend_ind['ma20']:,.2f} | "
        f"AboveMA={trend_ind['above_ma']} | "
        f"VWAP4h={'✅' if trend_ind['vwap_above'] else '⚠️'} ${trend_ind['vwap_4h']:,.2f}"
    )
    log.info(
        f"  Entry (1h): RSI={entry_ind['rsi']} | "
        f"Momentum={entry_ind['momentum']:.2f} | Signal={entry_ind['signal']}"
    )

def update_trailing_stop(price):
    """
    Trailing stop works in both directions.
    LONG:  stop only moves UP
    SHORT: stop only moves DOWN
    """
    global open_trade
    if not open_trade:
        return

    entry      = open_trade["entry"]
    tp         = open_trade["tp"]
    curr_stop  = open_trade["stop"]
    curr_level = open_trade["trail_level"]
    direction  = open_trade["direction"]
    full_range = abs(tp - entry)
    mode       = "[PAPER]" if PAPER else "[LIVE]"

    if direction == "long":
        progress = (price - entry) / full_range if full_range > 0 else 0

        if progress >= TRAIL_L3_PCT:
            new_stop = price * (1 - MIN_STOP_PCT)
            if new_stop > curr_stop:
                open_trade["stop"] = new_stop
                open_trade["trail_level"] = 3
                if curr_level < 3:
                    log.info(f"{mode} TRAIL L3 🔒 | {progress*100:.0f}% to TP | Stop trailing at ${new_stop:,.2f}")

        elif progress >= TRAIL_L2_PCT and curr_level < 2:
            new_stop = entry * 1.01
            if new_stop > curr_stop:
                open_trade["stop"] = new_stop
                open_trade["trail_level"] = 2
                log.info(f"{mode} TRAIL L2 🔒 | {progress*100:.0f}% to TP | Stop moved to ${new_stop:,.2f} (entry +1%)")

        elif progress >= TRAIL_L1_PCT and curr_level < 1:
            new_stop = entry
            if new_stop > curr_stop:
                open_trade["stop"] = new_stop
                open_trade["trail_level"] = 1
                log.info(f"{mode} TRAIL L1 🔒 | {progress*100:.0f}% to TP | Stop moved to breakeven ${new_stop:,.2f}")

    else:  # short
        progress = (entry - price) / full_range if full_range > 0 else 0

        if progress >= TRAIL_L3_PCT:
            new_stop = price * (1 + MIN_STOP_PCT)
            if new_stop < curr_stop:
                open_trade["stop"] = new_stop
                open_trade["trail_level"] = 3
                if curr_level < 3:
                    log.info(f"{mode} TRAIL L3 🔒 | {progress*100:.0f}% to TP | Stop trailing at ${new_stop:,.2f}")

        elif progress >= TRAIL_L2_PCT and curr_level < 2:
            new_stop = entry * 0.99
            if new_stop < curr_stop:
                open_trade["stop"] = new_stop
                open_trade["trail_level"] = 2
                log.info(f"{mode} TRAIL L2 🔒 | {progress*100:.0f}% to TP | Stop moved to ${new_stop:,.2f} (entry -1%)")

        elif progress >= TRAIL_L1_PCT and curr_level < 1:
            new_stop = entry
            if new_stop < curr_stop:
                open_trade["stop"] = new_stop
                open_trade["trail_level"] = 1
                log.info(f"{mode} TRAIL L1 🔒 | {progress*100:.0f}% to TP | Stop moved to breakeven ${new_stop:,.2f}")

def check_trade_exit(price):
    global open_trade, wins, losses, total_pnl, total_fees
    if not open_trade:
        return

    update_trailing_stop(price)

    direction = open_trade["direction"]
    fee_out   = price * open_trade["size"] * 0.001
    total_fee = open_trade["fee_in"] + fee_out
    mode      = "[PAPER]" if PAPER else "[LIVE]"
    dir_emoji = "📈" if direction == "long" else "📉"

    if direction == "long":
        stop_hit = price <= open_trade["stop"]
        tp_hit   = price >= open_trade["tp"]
        pnl_stop = (open_trade["stop"] - open_trade["entry"]) * open_trade["size"] - total_fee
        pnl_tp   = (open_trade["tp"] - open_trade["entry"]) * open_trade["size"] - total_fee
    else:
        stop_hit = price >= open_trade["stop"]
        tp_hit   = price <= open_trade["tp"]
        pnl_stop = (open_trade["entry"] - open_trade["stop"]) * open_trade["size"] - total_fee
        pnl_tp   = (open_trade["entry"] - open_trade["tp"]) * open_trade["size"] - total_fee

    if stop_hit:
        losses     += 1
        total_pnl  += pnl_stop
        total_fees += total_fee
        log.info(
            f"{mode} {dir_emoji} STOP HIT ❌ | Exit: ${price:,.2f} | "
            f"Net P&L: ${pnl_stop:,.2f} | Fees: ${total_fee:.2f} | "
            f"Trail Level: {open_trade['trail_level']}"
        )
        open_trade = None

    elif tp_hit:
        wins       += 1
        total_pnl  += pnl_tp
        total_fees += total_fee
        log.info(
            f"{mode} {dir_emoji} TP HIT ✅ | Exit: ${price:,.2f} | "
            f"Net P&L: ${pnl_tp:,.2f} | Fees: ${total_fee:.2f}"
        )
        open_trade = None

# ── Status Printer ────────────────────────────────────────────────────────────
def print_status(price, trend_direction, trend_ind, entry_ind):
    closed_trades = wins + losses
    open_count    = 1 if open_trade else 0
    total_trades  = closed_trades + open_count
    win_rate      = (wins / closed_trades * 100) if closed_trades else 0
    mode          = "PAPER" if PAPER else "LIVE"
    dir_label     = f"LONG bias" if trend_direction == "long" else f"SHORT bias" if trend_direction == "short" else "No bias"

    log.info(
        f"[{mode}] BTC: ${price:,.2f} | "
        f"20MA(4h): ${trend_ind.get('ma20', 0):,.2f} | "
        f"AboveMA: {trend_ind.get('above_ma', False)} | "
        f"VWAP4h↑: {trend_ind.get('vwap_above', False)} | "
        f"Bias: {dir_label}"
    )
    log.info(
        f"[{mode}] RSI(1h): {entry_ind.get('rsi', 0)} | "
        f"Momentum: {entry_ind.get('momentum', 0):.2f} | "
        f"LongSignal: {entry_ind.get('long_signal', False)} | "
        f"ShortSignal: {entry_ind.get('short_signal', False)}"
    )
    log.info(
        f"Trades: {total_trades} (Open: {open_count} | Closed: {closed_trades}) | "
        f"Wins: {wins} | Losses: {losses} | "
        f"Win Rate: {win_rate:.1f}% | P&L: ${total_pnl:,.2f} | Fees: ${total_fees:.2f}"
    )
    if open_trade:
        entry     = open_trade["entry"]
        tp        = open_trade["tp"]
        direction = open_trade["direction"]
        if direction == "long":
            progress = ((price - entry) / (tp - entry) * 100) if (tp - entry) > 0 else 0
        else:
            progress = ((entry - price) / (entry - tp) * 100) if (entry - tp) > 0 else 0
        dir_emoji = "📈" if direction == "long" else "📉"
        log.info(
            f"OPEN {dir_emoji} {direction.upper()} | Entry: ${entry:,.2f} | "
            f"Stop: ${open_trade['stop']:,.2f} | TP: ${tp:,.2f} | "
            f"Size: {open_trade['size']:.6f} BTC | "
            f"Progress: {progress:.1f}% to TP | "
            f"Trail Level: {open_trade['trail_level']}"
        )

# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    global tick_count
    log.info("=" * 60)
    log.info("Cipher Bot v5.7 — Two-Sided Trading (Long + Short)")
    log.info(f"Pair: {PAIR} | Mode: {'PAPER' if PAPER else 'LIVE'}")
    log.info(f"Trend Filter: 4h 20MA | LONG if above | SHORT if below")
    log.info(f"Long Entry:  RSI <60 + Positive Momentum")
    log.info(f"Short Entry: RSI >40 + Negative Momentum")
    log.info(f"Capital: ${CAPITAL:,.2f} | Risk: {RISK_PCT}% | TP: {TP_MULTI}x")
    log.info(f"Min Stop: {MIN_STOP_PCT*100:.1f}% | Max Size: {MAX_BTC_SIZE} BTC")
    log.info(f"Trailing: L1@33% breakeven | L2@75% +/-1% | L3@90% tight trail")
    log.info(f"Safety: One trade at a time — long OR short, never both")
    log.info("=" * 60)

    while True:
        try:
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

                trend_direction, trend_ind = check_trend(list(candles_4h))
                entry_signal, entry_ind    = check_entry(list(candles_1h), trend_direction)

                if open_trade:
                    check_trade_exit(price)

                if entry_signal and not open_trade:
                    signal_candle_1h = candles_1h[-2] if len(candles_1h) > 1 else candles_1h[-1]
                    if signal_candle_1h["time"] != last_signal_candle:
                        open_new_trade(
                            entry=price,
                            signal=entry_signal,
                            signal_candle_1h=signal_candle_1h,
                            trend_ind=trend_ind,
                            entry_ind=entry_ind
                        )
                    else:
                        log.info("Signal on same 1h candle — skipping re-entry.")

                if tick_count % 10 == 0:
                    print_status(price, trend_direction, trend_ind, entry_ind)

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
