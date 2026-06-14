

"""
Cipher Range Bot v1.1 — Ranging Market Companion Bot
Kraken Pro Paper Trading Bot
Runs 24/7 on Railway alongside Cipher Bot v5.7

Strategy:
- Detects when BTC is in a ranging/consolidating market
- Buys near range bottom, shorts near range top
- Tighter TP multiplier (1.25x) vs trend bot (2.5x)
- Same trailing stop structure as main bot
- Same risk management (2% per trade, 0.25 BTC cap)
- Sits out when market is clearly trending

Range Detection:
- Uses 4h candles to identify recent high/low over RANGE_LOOKBACK candles
- Range is valid when price oscillation < RANGE_WIDTH_PCT of price
- Enters long in bottom 25% of range, short in top 25% of range
- Exits range mode if price breaks outside range by BREAKOUT_PCT

v1.0 — Initial Build
- Two-sided ranging trades (long + short)
- ATR-based range validation
- Same trailing stop levels as main bot (L1/L2/L3)
- Signal candle lock to prevent duplicate entries
- Full logging matching main bot format
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
log = logging.getLogger("CipherRangeBot")

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY    = os.environ.get("KRAKEN_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")
PAIR       = os.environ.get("TRADING_PAIR", "XBTUSD")
CAPITAL    = float(os.environ.get("CAPITAL", "24000"))
RISK_PCT   = float(os.environ.get("RISK_PCT", "2"))
TP_MULTI   = float(os.environ.get("TP_MULTI", "1.25"))   # Tighter than trend bot
PAPER      = os.environ.get("PAPER_MODE", "true").lower() == "true"

# Timeframes
TF_TREND  = 240   # 4-hour candles — range detection
TF_ENTRY  = 60    # 1-hour candles — entry timing

TF_TREND_NEEDED  = 35   # enough candles for range detection
TF_ENTRY_NEEDED  = 20

# Range detection settings
RANGE_LOOKBACK   = 10     # number of 4h candles to define the range (~40 hours)
RANGE_WIDTH_PCT  = 0.06   # range must be < 6% wide to qualify as ranging
ENTRY_ZONE_PCT   = 0.25   # enter in bottom/top 25% of range
BREAKOUT_PCT     = 0.015  # 1.5% outside range = breakout, exit range mode

# Position size controls
MIN_STOP_PCT  = 0.015   # Minimum stop distance = 1.5% of entry price
MAX_BTC_SIZE  = 0.25    # Hard cap: never trade more than 0.25 BTC at once

# Trailing stop levels — same as main bot
TRAIL_L1_PCT  = 0.33   # 33% to TP → move stop to breakeven
TRAIL_L2_PCT  = 0.75   # 75% to TP → move stop to entry +/- 1%
TRAIL_L3_PCT  = 0.90   # 90% to TP → trail stop 1.5% from current price

# ── State ─────────────────────────────────────────────────────────────────────
candles_4h         = deque(maxlen=TF_TREND_NEEDED)
candles_1h         = deque(maxlen=TF_ENTRY_NEEDED)
open_trade         = None
tick_count         = 0
wins               = 0
losses             = 0
total_pnl          = 0.0
total_fees         = 0.0
last_signal_candle = None

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

def compute_atr(candle_list, period=10):
    """Average True Range — measures volatility"""
    if len(candle_list) < period + 1:
        return 0.0
    trs = []
    for i in range(-period, 0):
        c = candle_list[i]
        prev_close = candle_list[i-1]["close"]
        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - prev_close),
            abs(c["low"] - prev_close)
        )
        trs.append(tr)
    return sum(trs) / period

# ── Range Detection ───────────────────────────────────────────────────────────
def detect_range(candle_list_4h):
    """
    Identifies if BTC is in a ranging market.
    Returns range details or None if trending.

    A range is valid when:
    1. Recent high/low spread < RANGE_WIDTH_PCT of price
    2. ATR is relatively low (market not making large moves)
    """
    if len(candle_list_4h) < RANGE_LOOKBACK + 5:
        return None

    recent = candle_list_4h[-RANGE_LOOKBACK:]
    range_high = max(c["high"] for c in recent)
    range_low  = min(c["low"]  for c in recent)
    range_mid  = (range_high + range_low) / 2
    range_width_pct = (range_high - range_low) / range_mid

    atr = compute_atr(candle_list_4h, period=10)
    atr_pct = atr / range_mid

    is_ranging = range_width_pct <= RANGE_WIDTH_PCT

    return {
        "is_ranging":       is_ranging,
        "range_high":       round(range_high, 2),
        "range_low":        round(range_low, 2),
        "range_mid":        round(range_mid, 2),
        "range_width_pct":  round(range_width_pct * 100, 2),
        "atr":              round(atr, 2),
        "atr_pct":          round(atr_pct * 100, 2),
    }

def check_range_entry(price, range_info, candle_list_1h, candle_list_4h):
    """
    Determines entry signal within a confirmed range.

    TREND FILTER (v1.1):
    - Only LONG range trades when price is ABOVE 4h 20MA (uptrend)
    - Only SHORT range trades when price is BELOW 4h 20MA (downtrend)
    - Never trade counter-trend within a range

    LONG:  price above 20MA + in bottom zone + RSI < 45
    SHORT: price below 20MA + in top zone + RSI > 55
    """
    if not range_info or not range_info["is_ranging"]:
        return None, {}

    # Trend filter — compute 4h 20MA
    closes_4h = [c["close"] for c in candle_list_4h]
    ma20 = compute_ma(closes_4h, MA_PERIOD)
    above_ma = price > ma20

    rh = range_info["range_high"]
    rl = range_info["range_low"]
    full_range = rh - rl
    entry_zone = full_range * ENTRY_ZONE_PCT

    long_zone_top  = rl + entry_zone
    short_zone_bot = rh - entry_zone

    closes = [c["close"] for c in candle_list_1h]
    rsi = compute_rsi(closes)

    in_long_zone  = price <= long_zone_top
    in_short_zone = price >= short_zone_bot
    rsi_long_ok   = rsi < 45
    rsi_short_ok  = rsi > 55

    # Apply trend filter — only trade WITH the trend
    long_signal  = in_long_zone  and rsi_long_ok  and above_ma
    short_signal = in_short_zone and rsi_short_ok and not above_ma

    if long_signal:
        signal = "BUY"
    elif short_signal:
        signal = "SELL"
    else:
        signal = None

    return signal, {
        "rsi":            round(rsi, 1),
        "ma20":           round(ma20, 2),
        "above_ma":       above_ma,
        "long_zone_top":  round(long_zone_top, 2),
        "short_zone_bot": round(short_zone_bot, 2),
        "in_long_zone":   in_long_zone,
        "in_short_zone":  in_short_zone,
        "long_signal":    long_signal,
        "short_signal":   short_signal,
        "signal":         signal,
    }

def check_breakout(price, range_info):
    """Returns True if price has broken outside the range — exit range mode."""
    if not range_info:
        return False
    rh = range_info["range_high"]
    rl = range_info["range_low"]
    upper_break = price > rh * (1 + BREAKOUT_PCT)
    lower_break = price < rl * (1 - BREAKOUT_PCT)
    return upper_break or lower_break

# ── Trade Management ──────────────────────────────────────────────────────────
def open_new_trade(entry, signal, signal_candle_1h, range_info, entry_ind):
    global open_trade, last_signal_candle

    direction = "long" if signal == "BUY" else "short"

    stop = entry * (1 - MIN_STOP_PCT) if direction == "long" else entry * (1 + MIN_STOP_PCT)
    risk = abs(entry - stop)

    if risk <= 0:
        log.warning("Risk <= 0, skipping trade.")
        return

    tp = entry + (risk * TP_MULTI) if direction == "long" else entry - (risk * TP_MULTI)

    raw_size     = (CAPITAL * RISK_PCT / 100) / risk
    size         = min(raw_size, MAX_BTC_SIZE)
    fee_in       = entry * size * 0.001
    risk_dollars = CAPITAL * RISK_PCT / 100
    fee_pct_risk = (fee_in / risk_dollars) * 100
    stop_dist    = abs(entry - stop)
    stop_pct     = (stop_dist / entry) * 100

    mode      = "[PAPER]" if PAPER else "[LIVE]"
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
        f"{mode} {dir_emoji} RANGE {direction.upper()} OPENED | Entry: ${entry:,.2f} | "
        f"Stop: ${stop:,.2f} | TP: ${tp:,.2f} | "
        f"Size: {size:.6f} BTC | Fee-in: ${fee_in:.2f}"
    )
    log.info(
        f"  Stop dist: ${stop_dist:,.2f} ({stop_pct:.2f}%) | "
        f"Raw size: {raw_size:.4f} → Capped: {size:.4f} BTC | "
        f"Fee as % of risk: {fee_pct_risk:.1f}%"
    )
    log.info(
        f"  Range: ${range_info['range_low']:,.2f} — ${range_info['range_high']:,.2f} | "
        f"Width: {range_info['range_width_pct']}% | ATR: ${range_info['atr']:,.2f}"
    )
    log.info(
        f"  Entry (1h): RSI={entry_ind['rsi']} | "
        f"LongZone: {entry_ind['in_long_zone']} | ShortZone: {entry_ind['in_short_zone']}"
    )

def update_trailing_stop(price):
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

    else:
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

def check_trade_exit(price, range_info):
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
        pnl_tp   = (open_trade["tp"]   - open_trade["entry"]) * open_trade["size"] - total_fee
    else:
        stop_hit = price >= open_trade["stop"]
        tp_hit   = price <= open_trade["tp"]
        pnl_stop = (open_trade["entry"] - open_trade["stop"]) * open_trade["size"] - total_fee
        pnl_tp   = (open_trade["entry"] - open_trade["tp"])   * open_trade["size"] - total_fee

    # Also exit if range has broken out
    breakout = check_breakout(price, range_info)

    if tp_hit:
        wins       += 1
        total_pnl  += pnl_tp
        total_fees += total_fee
        log.info(
            f"{mode} {dir_emoji} TP HIT ✅ | Exit: ${price:,.2f} | "
            f"Net P&L: ${pnl_tp:,.2f} | Fees: ${total_fee:.2f}"
        )
        open_trade = None

    elif stop_hit:
        # Count as win if profitable, loss if not
        if pnl_stop > 0:
            wins += 1
        else:
            losses += 1
        total_pnl  += pnl_stop
        total_fees += total_fee
        log.info(
            f"{mode} {dir_emoji} STOP HIT {'✅' if pnl_stop > 0 else '❌'} | Exit: ${price:,.2f} | "
            f"Net P&L: ${pnl_stop:,.2f} | Fees: ${total_fee:.2f} | "
            f"Trail Level: {open_trade['trail_level']}"
        )
        open_trade = None

    elif breakout:
        # Range broken — exit immediately at market
        pnl = ((price - open_trade["entry"]) if direction == "long"
                else (open_trade["entry"] - price)) * open_trade["size"] - total_fee
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        total_pnl  += pnl
        total_fees += total_fee
        log.info(
            f"{mode} {dir_emoji} BREAKOUT EXIT ⚡ | Exit: ${price:,.2f} | "
            f"Net P&L: ${pnl:,.2f} | Fees: ${total_fee:.2f}"
        )
        open_trade = None

# ── Status Printer ────────────────────────────────────────────────────────────
def print_status(price, range_info, entry_ind):
    closed_trades = wins + losses
    open_count    = 1 if open_trade else 0
    total_trades  = closed_trades + open_count
    win_rate      = (wins / closed_trades * 100) if closed_trades else 0
    mode          = "PAPER" if PAPER else "LIVE"

    if range_info:
        range_status = (f"RANGING ${range_info['range_low']:,.2f}—${range_info['range_high']:,.2f} "
                       f"({range_info['range_width_pct']}%)"
                       if range_info["is_ranging"] else
                       f"TRENDING (range {range_info['range_width_pct']}% > {RANGE_WIDTH_PCT*100}% threshold)")
    else:
        range_status = "DETECTING..."

    log.info(
        f"[{mode}] BTC: ${price:,.2f} | {range_status}"
    )
    if entry_ind:
        log.info(
            f"[{mode}] RSI(1h): {entry_ind.get('rsi', 0)} | "
            f"20MA(4h): ${entry_ind.get('ma20', 0):,.2f} | "
            f"AboveMA: {entry_ind.get('above_ma', False)} | "
            f"LongZone≤${entry_ind.get('long_zone_top', 0):,.2f} | "
            f"ShortZone≥${entry_ind.get('short_zone_bot', 0):,.2f} | "
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
            f"OPEN {dir_emoji} RANGE {direction.upper()} | Entry: ${entry:,.2f} | "
            f"Stop: ${open_trade['stop']:,.2f} | TP: ${tp:,.2f} | "
            f"Size: {open_trade['size']:.6f} BTC | "
            f"Progress: {progress:.1f}% to TP | "
            f"Trail Level: {open_trade['trail_level']}"
        )

# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    global tick_count
    log.info("=" * 60)
    log.info("Cipher Range Bot v1.1 — Ranging Market Companion")
    log.info(f"Pair: {PAIR} | Mode: {'PAPER' if PAPER else 'LIVE'}")
    log.info(f"Range Detection: {RANGE_LOOKBACK} x 4h candles | Width < {RANGE_WIDTH_PCT*100}%")
    log.info(f"Entry Zones: Bottom/Top {ENTRY_ZONE_PCT*100}% of range")
    log.info(f"Long Entry:  Price in bottom zone + RSI < 45")
    log.info(f"Short Entry: Price in top zone + RSI > 55")
    log.info(f"Capital: ${CAPITAL:,.2f} | Risk: {RISK_PCT}% | TP: {TP_MULTI}x")
    log.info(f"Min Stop: {MIN_STOP_PCT*100:.1f}% | Max Size: {MAX_BTC_SIZE} BTC")
    log.info(f"Trailing: L1@33% breakeven | L2@75% +/-1% | L3@90% tight trail")
    log.info(f"Breakout Exit: {BREAKOUT_PCT*100}% outside range")
    log.info(f"Runs alongside Cipher Bot v5.7 — independent paper trading")
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

                range_info  = detect_range(list(candles_4h))
                entry_signal, entry_ind = check_range_entry(price, range_info, list(candles_1h), list(candles_4h))

                if open_trade:
                    check_trade_exit(price, range_info)

                if entry_signal and not open_trade:
                    signal_candle_1h = candles_1h[-2] if len(candles_1h) > 1 else candles_1h[-1]
                    if signal_candle_1h["time"] != last_signal_candle:
                        open_new_trade(
                            entry=price,
                            signal=entry_signal,
                            signal_candle_1h=signal_candle_1h,
                            range_info=range_info,
                            entry_ind=entry_ind
                        )
                    else:
                        log.info("Signal on same 1h candle — skipping re-entry.")

                if tick_count % 10 == 0:
                    print_status(price, range_info, entry_ind)

            tick_count += 1
            time.sleep(30)

        except KeyboardInterrupt:
            log.info("Cipher Range Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
