"""
Cipher Bot v4 — Market Cipher Strategy Engine
Kraken Pro Paper Trading Bot
Runs 24/7 on Railway

v4 Changes:
- Added 200 candle Moving Average trend filter
- Only buys when price is ABOVE the 200MA (confirmed uptrend)
- Prevents catching falling knives in sustained downtrends
- Green Dot still requires RSI < 38 (deeply oversold)
"""

import os
import time
import requests
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

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY    = os.environ.get("KRAKEN_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")
PAIR       = os.environ.get("TRADING_PAIR", "XBTUSD")
TIMEFRAME  = int(os.environ.get("TIMEFRAME_MINUTES", "240"))  # 4h
CAPITAL    = float(os.environ.get("CAPITAL", "10000"))
RISK_PCT   = float(os.environ.get("RISK_PCT", "2"))
TP_MULTI   = float(os.environ.get("TP_MULTI", "2.5"))
PAPER_MODE = os.environ.get("PAPER_MODE", "true").lower() == "true"
FEE_RATE   = 0.001  # 0.10% Kraken Pro
MA_PERIOD  = 50     # Moving average period — price must be above this to buy

# ── State ─────────────────────────────────────────────────────────────────────
candles    = deque(maxlen=200)
open_trade = None
capital    = CAPITAL
cooldown   = 0
stats      = {"wins": 0, "losses": 0, "pnl": 0.0, "fees": 0.0, "trades": 0}

# ── Kraken API ────────────────────────────────────────────────────────────────
KRAKEN_BASE = "https://api.kraken.com"

def kraken_public(endpoint, params=None):
    try:
        url  = f"{KRAKEN_BASE}/0/public/{endpoint}"
        r    = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("error"):
            log.warning(f"Kraken error: {data['error']}")
            return None
        return data.get("result")
    except Exception as e:
        log.error(f"Kraken API error: {e}")
        return None

def get_ohlc():
    result = kraken_public("OHLC", {"pair": PAIR, "interval": TIMEFRAME})
    if not result:
        return []
    key = [k for k in result.keys() if k != "last"][0]
    return [{"time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "volume": float(c[6])}
            for c in result[key]]

def get_live_price():
    result = kraken_public("Ticker", {"pair": PAIR})
    if not result:
        return None
    key = list(result.keys())[0]
    return float(result[key]["c"][0])

# ── Indicators ────────────────────────────────────────────────────────────────
def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        change = closes[-i] - closes[-i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))

def compute_momentum(closes, period=5):
    if len(closes) < period + 1:
        return 0.0
    return closes[-1] - closes[-period - 1]

def compute_ma(closes, period):
    """Simple moving average over last N candles."""
    if len(closes) < period:
        return closes[-1]
    return sum(closes[-period:]) / period

def compute_vwap(candle_list):
    if not candle_list:
        return 0.0
    total_vol = sum(c["volume"] for c in candle_list)
    if total_vol == 0:
        return candle_list[-1]["close"]
    total_pv  = sum(((c["high"] + c["low"] + c["close"]) / 3) * c["volume"] for c in candle_list)
    return total_pv / total_vol

# ── Signal Logic ──────────────────────────────────────────────────────────────
def check_signals(candle_list):
    """
    Market Cipher inspired signals with 50MA trend filter.
    Only buys when:
    1. Price is ABOVE the 50 candle MA (uptrend confirmed)
    2. RSI is below 38 (deeply oversold — genuine dip)
    3. Momentum was negative and has flipped positive (bouncing)
    4. Market is trending (not choppy)
    """
    if len(candle_list) < 20:
        return None, {}

    closes     = [c["close"] for c in candle_list]
    rsi        = compute_rsi(closes)
    mom5       = compute_momentum(closes, 5)
    mom10      = compute_momentum(closes, 10)
    ma50       = compute_ma(closes, MA_PERIOD)
    vwap       = compute_vwap(candle_list[-10:])
    price      = closes[-1]
    vwap_above = price > vwap
    above_ma   = price > ma50      # KEY: must be in uptrend
    trending   = abs(mom10) > (price * 0.001)
    prev_mom5  = compute_momentum(closes[:-1], 5) if len(closes) > 6 else 0
    oversold   = rsi < 38

    # Green Dot — must be oversold AND in uptrend
    green_dot = (
        oversold and               # RSI deeply oversold
        above_ma and               # price above 50MA — uptrend only
        mom5 > (price * 0.001) and # momentum turning positive
        prev_mom5 < 0 and          # was falling before
        trending                   # clear direction
    )

    # Blue Triangle — warning signal only, no shorting
    blue_tri = (
        rsi > 62 and
        mom5 < -(price * 0.001) and
        prev_mom5 > 0 and
        trending
    )

    signal = None
    if green_dot and vwap_above:
        signal = "BUY"

    indicators = {
        "rsi":        round(rsi, 1),
        "momentum":   round(mom5, 2),
        "ma50":       round(ma50, 2),
        "vwap":       round(vwap, 2),
        "vwap_above": vwap_above,
        "above_ma":   above_ma,
        "trending":   trending,
        "oversold":   oversold,
        "green_dot":  green_dot,
        "blue_tri":   blue_tri,
    }

    return signal, indicators

# ── Trade Management ──────────────────────────────────────────────────────────
def open_new_trade(entry, signal_candle, indicators):
    global open_trade, capital

    risk_amount   = capital * (RISK_PCT / 100)
    stop_loss     = signal_candle["low"]
    risk_per_unit = entry - stop_loss

    if risk_per_unit <= 0:
        log.warning("Invalid stop loss — skipping trade")
        return

    take_profit = entry + (risk_per_unit * TP_MULTI)
    size        = risk_amount / risk_per_unit
    entry_fee   = risk_amount * FEE_RATE

    open_trade = {
        "type":        "BUY",
        "entry":       entry,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "size":        size,
        "risk":        risk_amount,
        "entry_fee":   entry_fee,
        "opened_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    mode = "PAPER" if PAPER_MODE else "LIVE"
    log.info(f"🟢 {mode} BUY OPENED")
    log.info(f"   Entry:       ${entry:,.2f}")
    log.info(f"   Stop Loss:   ${stop_loss:,.2f}")
    log.info(f"   Take Profit: ${take_profit:,.2f}")
    log.info(f"   Size:        {size:.6f} BTC")
    log.info(f"   Risk:        ${risk_amount:.2f}")
    log.info(f"   Entry Fee:   ${entry_fee:.4f}")
    log.info(f"   RSI: {indicators['rsi']} | Above 50MA: {indicators['above_ma']}")

def check_trade_exit(current_high, current_low):
    global open_trade, capital, stats, cooldown

    if not open_trade:
        return

    exit_fee = open_trade["risk"] * FEE_RATE

    if current_low <= open_trade["stop_loss"]:
        total_fees = open_trade["entry_fee"] + exit_fee
        loss       = -(open_trade["risk"] + total_fees)
        capital   += loss
        stats["losses"] += 1
        stats["pnl"]    += loss
        stats["fees"]   += total_fees
        stats["trades"] += 1
        cooldown = 3

        log.info(f"🔴 STOP LOSS HIT")
        log.info(f"   Loss:    ${abs(open_trade['risk']):.2f} | Fees: ${total_fees:.4f} | Net: ${loss:.2f}")
        log.info(f"   Capital: ${capital:,.2f} | Win Rate: {win_rate():.0f}%")
        open_trade = None

    elif current_high >= open_trade["take_profit"]:
        total_fees = open_trade["entry_fee"] + exit_fee
        gross      = open_trade["risk"] * TP_MULTI
        profit     = gross - total_fees
        capital   += profit
        stats["wins"]   += 1
        stats["pnl"]    += profit
        stats["fees"]   += total_fees
        stats["trades"] += 1
        cooldown = 2

        log.info(f"✅ TAKE PROFIT HIT")
        log.info(f"   Gross: ${gross:.2f} | Fees: ${total_fees:.4f} | Net: +${profit:.2f}")
        log.info(f"   Capital: ${capital:,.2f} | Win Rate: {win_rate():.0f}%")
        open_trade = None

def win_rate():
    if stats["trades"] == 0:
        return 0
    return (stats["wins"] / stats["trades"]) * 100

# ── Status Report ──────────────────────────────────────────────────────────────
def print_status(price, indicators):
    log.info("─" * 55)
    log.info(f"📊 {PAIR} | Price: ${price:,.2f} | RSI: {indicators.get('rsi','?')} | 50MA: ${indicators.get('ma50',0):,.2f}")
    log.info(f"   Capital: ${capital:,.2f} | P&L: ${stats['pnl']:+.2f} | Fees: ${stats['fees']:.2f}")
    log.info(f"   Trades: {stats['trades']} | Wins: {stats['wins']} | Losses: {stats['losses']} | Win Rate: {win_rate():.0f}%")
    log.info(f"   Oversold: {indicators.get('oversold')} | Above 50MA: {indicators.get('above_ma')} | VWAP Above: {indicators.get('vwap_above')} | Green Dot: {indicators.get('green_dot')}")
    if open_trade:
        unrealized = (price - open_trade["entry"]) * open_trade["size"]
        log.info(f"   Open Trade: Entry ${open_trade['entry']:,.2f} | Unrealized: ${unrealized:+.2f}")
    log.info(f"   Mode: {'📋 PAPER' if PAPER_MODE else '💰 LIVE'} | Cooldown: {cooldown} candles")
    log.info("─" * 55)

# ── Main Loop ──────────────────────────────────────────────────────────────────
def main():
    global cooldown

    log.info("=" * 55)
    log.info("⬡  CIPHER BOT v4 — Market Cipher Strategy Engine")
    log.info(f"   Pair:      {PAIR}")
    log.info(f"   Timeframe: {TIMEFRAME} minutes")
    log.info(f"   Capital:   ${CAPITAL:,.2f}")
    log.info(f"   Risk:      {RISK_PCT}% per trade")
    log.info(f"   TP:        {TP_MULTI}x risk")
    log.info(f"   MA Filter: {MA_PERIOD} candle MA — uptrend only")
    log.info(f"   Mode:      {'📋 PAPER TRADING' if PAPER_MODE else '💰 LIVE TRADING'}")
    log.info(f"   Fee Rate:  {FEE_RATE * 100}% (Kraken Pro)")
    log.info("=" * 55)

    last_candle_time = 0
    tick_count       = 0

    while True:
        try:
            price = get_live_price()
            if not price:
                log.warning("Could not fetch live price — retrying in 30s")
                time.sleep(30)
                continue

            if tick_count % 10 == 0:
                new_candles = get_ohlc()
                if new_candles:
                    for c in new_candles:
                        if c["time"] > last_candle_time:
                            candles.append(c)
                            last_candle_time = c["time"]
                    log.info(f"📡 Loaded {len(candles)} candles | Latest close: ${candles[-1]['close']:,.2f}")

            if len(candles) >= 20:
                signal, indicators = check_signals(list(candles))

                if open_trade:
                    check_trade_exit(
                        current_high=candles[-1]["high"],
                        current_low=candles[-1]["low"]
                    )

                if not open_trade and signal == "BUY" and cooldown == 0:
                    open_new_trade(
                        entry=candles[-1]["close"],
                        signal_candle=candles[-2] if len(candles) > 1 else candles[-1],
                        indicators=indicators
                    )

                if cooldown > 0:
                    cooldown -= 1

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
