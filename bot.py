"""
Cipher Bot — Market Cipher Strategy Engine
Kraken Pro Paper Trading Bot
Runs 24/7 on Railway
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

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("CipherBot")

# ── Config from environment variables ────────────────────────────────────────
API_KEY    = os.environ.get("KRAKEN_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")
PAIR       = os.environ.get("TRADING_PAIR", "XBTUSD")
TIMEFRAME  = int(os.environ.get("TIMEFRAME_MINUTES", "60"))   # 1h for wider range markets
CAPITAL    = float(os.environ.get("CAPITAL", "10000"))
RISK_PCT   = float(os.environ.get("RISK_PCT", "2"))
TP_MULTI   = float(os.environ.get("TP_MULTI", "2.5"))          # 2.5x to catch bigger swings
PAPER_MODE = os.environ.get("PAPER_MODE", "true").lower() == "true"
FEE_RATE   = 0.001  # 0.10% Kraken Pro

# ── State ─────────────────────────────────────────────────────────────────────
candles     = deque(maxlen=100)
open_trade  = None
capital     = CAPITAL
cooldown    = 0
stats       = {"wins": 0, "losses": 0, "pnl": 0.0, "fees": 0.0, "trades": 0}

# ── Kraken API ────────────────────────────────────────────────────────────────
KRAKEN_BASE = "https://api.kraken.com"

def kraken_public(endpoint, params=None):
    """Call a public Kraken API endpoint."""
    try:
        url = f"{KRAKEN_BASE}/0/public/{endpoint}"
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("error"):
            log.warning(f"Kraken API error: {data['error']}")
            return None
        return data.get("result")
    except Exception as e:
        log.error(f"Kraken public API error: {e}")
        return None

def get_ohlc():
    """Fetch OHLC candle data from Kraken."""
    result = kraken_public("OHLC", {"pair": PAIR, "interval": TIMEFRAME})
    if not result:
        return []
    key = [k for k in result.keys() if k != "last"][0]
    raw = result[key]
    parsed = []
    for c in raw:
        parsed.append({
            "time":   int(c[0]),
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[6]),
        })
    return parsed

def get_live_price():
    """Get the current live Bitcoin price from Kraken."""
    result = kraken_public("Ticker", {"pair": PAIR})
    if not result:
        return None
    key = list(result.keys())[0]
    return float(result[key]["c"][0])

# ── Signal Logic ──────────────────────────────────────────────────────────────
def compute_rsi(closes, period=14):
    """Simple RSI calculation."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        change = closes[-i] - closes[-i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_momentum(closes, period=5):
    """Simple momentum — difference between current and n periods ago."""
    if len(closes) < period + 1:
        return 0.0
    return closes[-1] - closes[-period - 1]

def compute_vwap(candle_list):
    """Simple VWAP over provided candles."""
    if not candle_list:
        return 0.0
    total_vol = sum(c["volume"] for c in candle_list)
    if total_vol == 0:
        return candle_list[-1]["close"]
    total_pv = sum(((c["high"] + c["low"] + c["close"]) / 3) * c["volume"] for c in candle_list)
    return total_pv / total_vol

def check_signals(candle_list):
    """
    Market Cipher inspired signal logic.
    Returns: "BUY", "SELL", or None
    """
    if len(candle_list) < 15:
        return None, {}

    closes = [c["close"] for c in candle_list]

    # Indicators
    rsi        = compute_rsi(closes)
    mom5       = compute_momentum(closes, 5)
    mom10      = compute_momentum(closes, 10)
    vwap       = compute_vwap(candle_list[-10:])
    price      = closes[-1]
    vwap_above = price > vwap
    trending   = abs(mom10) > (price * 0.0008)  # loosened to 0.08% — catches more moves in wide range

    # Green Dot — bullish reversal — loosened momentum threshold
    prev_mom5 = compute_momentum(closes[:-1], 5) if len(closes) > 6 else 0
    green_dot = (
        mom5 > (price * 0.0015) and  # loosened from 0.002 to 0.0015
        prev_mom5 < 0 and             # was negative before
        25 < rsi < 58 and             # slightly wider RSI range
        trending
    )

    # Blue Triangle — bearish reversal — loosened momentum threshold
    blue_tri = (
        mom5 < -(price * 0.0015) and  # loosened from 0.002 to 0.0015
        prev_mom5 > 0 and
        42 < rsi < 78 and             # slightly wider RSI range
        trending
    )

    signal = None
    if green_dot and vwap_above:
        signal = "BUY"
    elif blue_tri and not vwap_above:
        signal = "SELL"

    indicators = {
        "rsi": round(rsi, 1),
        "momentum": round(mom5, 2),
        "vwap": round(vwap, 2),
        "vwap_above": vwap_above,
        "trending": trending,
        "green_dot": green_dot,
        "blue_tri": blue_tri,
    }

    return signal, indicators

# ── Trade Management ──────────────────────────────────────────────────────────
def open_new_trade(entry, signal_candle, indicators):
    """Open a new paper trade."""
    global open_trade, capital

    risk_amount = capital * (RISK_PCT / 100)
    stop_loss   = signal_candle["low"]
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
    log.info(f"   RSI: {indicators['rsi']} | Trending: {indicators['trending']}")

def check_trade_exit(current_high, current_low):
    """Check if open trade should be closed."""
    global open_trade, capital, stats, cooldown

    if not open_trade:
        return

    exit_fee = open_trade["risk"] * FEE_RATE

    # Stop loss hit
    if current_low <= open_trade["stop_loss"]:
        total_fees = open_trade["entry_fee"] + exit_fee
        loss       = -(open_trade["risk"] + total_fees)
        capital   += loss
        stats["losses"] += 1
        stats["pnl"]    += loss
        stats["fees"]   += total_fees
        stats["trades"] += 1
        cooldown = 3  # reduced from 5 to 3 candles after loss

        log.info(f"🔴 STOP LOSS HIT")
        log.info(f"   Loss:       ${abs(open_trade['risk']):.2f}")
        log.info(f"   Fees:       ${total_fees:.4f}")
        log.info(f"   Net:        ${loss:.2f}")
        log.info(f"   Capital:    ${capital:,.2f}")
        log.info(f"   Win Rate:   {win_rate():.0f}%")
        open_trade = None

    # Take profit hit
    elif current_high >= open_trade["take_profit"]:
        total_fees = open_trade["entry_fee"] + exit_fee
        gross      = open_trade["risk"] * TP_MULTI
        profit     = gross - total_fees
        capital   += profit
        stats["wins"]   += 1
        stats["pnl"]    += profit
        stats["fees"]   += total_fees
        stats["trades"] += 1
        cooldown = 2  # reduced from 3 to 2 candles after win

        log.info(f"✅ TAKE PROFIT HIT")
        log.info(f"   Gross:      ${gross:.2f}")
        log.info(f"   Fees:       ${total_fees:.4f}")
        log.info(f"   Net:        +${profit:.2f}")
        log.info(f"   Capital:    ${capital:,.2f}")
        log.info(f"   Win Rate:   {win_rate():.0f}%")
        open_trade = None

def win_rate():
    """Calculate current win rate."""
    if stats["trades"] == 0:
        return 0
    return (stats["wins"] / stats["trades"]) * 100

# ── Status Report ─────────────────────────────────────────────────────────────
def print_status(price, indicators):
    """Print a regular status update."""
    log.info("─" * 50)
    log.info(f"📊 {PAIR} | Price: ${price:,.2f} | RSI: {indicators.get('rsi', '?')}")
    log.info(f"   Capital: ${capital:,.2f} | P&L: ${stats['pnl']:+.2f} | Fees: ${stats['fees']:.2f}")
    log.info(f"   Trades: {stats['trades']} | Wins: {stats['wins']} | Losses: {stats['losses']} | Win Rate: {win_rate():.0f}%")
    log.info(f"   Green Dot: {indicators.get('green_dot')} | VWAP Above: {indicators.get('vwap_above')} | Trending: {indicators.get('trending')}")
    if open_trade:
        unrealized = (price - open_trade["entry"]) * open_trade["size"]
        log.info(f"   Open Trade: Entry ${open_trade['entry']:,.2f} | Unrealized P&L: ${unrealized:+.2f}")
    log.info(f"   Mode: {'📋 PAPER' if PAPER_MODE else '💰 LIVE'} | Cooldown: {cooldown} candles")
    log.info("─" * 50)

# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    global cooldown, open_trade

    log.info("=" * 50)
    log.info("⬡  CIPHER BOT — Market Cipher Strategy Engine")
    log.info(f"   Pair:      {PAIR}")
    log.info(f"   Timeframe: {TIMEFRAME} minutes")
    log.info(f"   Capital:   ${CAPITAL:,.2f}")
    log.info(f"   Risk:      {RISK_PCT}% per trade")
    log.info(f"   TP:        {TP_MULTI}x risk")
    log.info(f"   Mode:      {'📋 PAPER TRADING' if PAPER_MODE else '💰 LIVE TRADING'}")
    log.info(f"   Fee Rate:  {FEE_RATE * 100}% (Kraken Pro)")
    log.info("=" * 50)

    last_candle_time = 0
    tick_count       = 0

    while True:
        try:
            # Fetch live price every 30 seconds
            price = get_live_price()
            if not price:
                log.warning("Could not fetch live price — retrying in 30s")
                time.sleep(30)
                continue

            # Fetch new candles every 5 minutes
            if tick_count % 10 == 0:
                new_candles = get_ohlc()
                if new_candles:
                    # Only add new candles we haven't seen
                    for c in new_candles:
                        if c["time"] > last_candle_time:
                            candles.append(c)
                            last_candle_time = c["time"]
                    log.info(f"📡 Loaded {len(candles)} candles | Latest close: ${candles[-1]['close']:,.2f}")

            # Check signals
            if len(candles) >= 15:
                signal, indicators = check_signals(list(candles))

                # Check open trade exits first
                if open_trade:
                    check_trade_exit(
                        current_high=candles[-1]["high"],
                        current_low=candles[-1]["low"]
                    )

                # Check for new entry
                if not open_trade and signal == "BUY" and cooldown == 0:
                    open_new_trade(
                        entry=candles[-1]["close"],
                        signal_candle=candles[-2] if len(candles) > 1 else candles[-1],
                        indicators=indicators
                    )

                # Tick down cooldown
                if cooldown > 0:
                    cooldown -= 1

                # Print status every 5 minutes
                if tick_count % 10 == 0:
                    print_status(price, indicators)

            tick_count += 1
            time.sleep(30)  # Check every 30 seconds

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
