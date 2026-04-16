"""
Cipher Bot v5.1 — Market Cipher Strategy Engine
Kraken Pro Paper Trading Bot
Runs 24/7 on Railway

v5.1 Changes:
- RSI threshold raised from 45 to 55 (catches more setups in bull market conditions)
- All other v5 parameters unchanged: 50MA filter, momentum 0.05% of price

v5 Parameters:
- 50MA trend filter
- RSI threshold: below 55 (updated from 45)
- Momentum threshold: 0.05% of price
- Capital: $17,500 | Risk: 2% per trade | TP: 2.5x
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

PAIR          = "XBTUSD"
TIMEFRAME     = 240          # 4-hour candles (in minutes)
CANDLES_NEEDED = 60          # enough for 50MA + RSI
PAPER         = True         # paper trading mode

CAPITAL       = 17500.00
RISK_PCT      = 0.02         # 2% risk per trade
TP_MULTI      = 2.5          # take profit = 2.5x the risk

# ── v5.1 Signal Parameters ────────────────────────────────────────────────────
MA_PERIOD          = 50
RSI_PERIOD         = 14
RSI_THRESHOLD      = 55      # ← CHANGED from 45 to 55
MOMENTUM_THRESHOLD = 0.0005  # 0.05% of price

# ── State ─────────────────────────────────────────────────────────────────────
candles    = deque(maxlen=200)
open_trade = None
tick_count = 0
cooldown   = 0

trade_log  = []
wins       = 0
losses     = 0
total_pnl  = 0.0
total_fees = 0.0

# ── Kraken REST helpers ───────────────────────────────────────────────────────
BASE = "https://api.kraken.com/0/public"

def get_candles():
    try:
        r = requests.get(
            f"{BASE}/OHLC",
            params={"pair": PAIR, "interval": TIMEFRAME},
            timeout=10
        )
        data = r.json()
        if data.get("error"):
            log.error(f"Kraken OHLC error: {data['error']}")
            return []
        raw = list(data["result"].values())[0]
        return [
            {
                "time":  int(c[0]),
                "open":  float(c[1]),
                "high":  float(c[2]),
                "low":   float(c[3]),
                "close": float(c[4]),
                "vol":   float(c[6]),
            }
            for c in raw[:-1]   # drop the still-forming candle
        ]
    except Exception as e:
        log.error(f"get_candles error: {e}")
        return []

def get_price():
    try:
        r = requests.get(
            f"{BASE}/Ticker",
            params={"pair": PAIR},
            timeout=10
        )
        data = r.json()
        if data.get("error"):
            return None
        ticker = list(data["result"].values())[0]
        return float(ticker["c"][0])
    except Exception as e:
        log.error(f"get_price error: {e}")
        return None

# ── Indicators ────────────────────────────────────────────────────────────────
def calc_sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses_list = [], []
    for i in range(1, period + 1):
        diff = closes[-period + i] - closes[-period + i - 1]
        (gains if diff > 0 else losses_list).append(abs(diff))
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses_list) / period if losses_list else 1e-9
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_vwap(candle_list):
    """Simple session VWAP over available candles."""
    total_vol = sum(c["vol"] for c in candle_list)
    if total_vol == 0:
        return None
    return sum(((c["high"] + c["low"] + c["close"]) / 3) * c["vol"]
               for c in candle_list) / total_vol

# ── Signal Logic ──────────────────────────────────────────────────────────────
def check_signals(candle_list):
    closes = [c["close"] for c in candle_list]

    ma_val   = calc_sma(closes, MA_PERIOD)
    rsi_val  = calc_rsi(closes, RSI_PERIOD)
    vwap_val = calc_vwap(candle_list)

    if ma_val is None or rsi_val is None or vwap_val is None:
        return None, {}

    price = closes[-1]

    # Momentum: difference between last two closes
    momentum = closes[-1] - closes[-2] if len(closes) >= 2 else 0
    momentum_threshold = price * MOMENTUM_THRESHOLD

    above_ma   = price > ma_val
    oversold   = rsi_val < RSI_THRESHOLD
    vwap_up    = price > vwap_val
    mom_flip   = momentum > momentum_threshold

    green_dot  = oversold and vwap_up and mom_flip

    indicators = {
        "price":    price,
        "ma":       ma_val,
        "rsi":      rsi_val,
        "oversold": oversold,
        "above_ma": above_ma,
        "vwap_up":  vwap_up,
        "green_dot": green_dot,
    }

    signal = "BUY" if (above_ma and green_dot) else None
    return signal, indicators

# ── Trade Management ──────────────────────────────────────────────────────────
def open_new_trade(entry, signal_candle, indicators):
    global open_trade, cooldown

    stop   = signal_candle["low"]
    risk   = entry - stop
    if risk <= 0:
        return

    risk_dollars = CAPITAL * RISK_PCT
    size         = risk_dollars / risk
    tp           = entry + (risk * TP_MULTI)
    fee          = size * entry * 0.0026   # Kraken taker ~0.26%

    open_trade = {
        "entry": entry,
        "stop":  stop,
        "tp":    tp,
        "size":  size,
        "fee_open": fee,
        "time":  datetime.utcnow().isoformat(),
    }
    cooldown = 3
    log.info(
        f"[{'PAPER' if PAPER else 'LIVE'}] TRADE OPENED | "
        f"Entry: ${entry:,.2f} | Stop: ${stop:,.2f} | "
        f"TP: ${tp:,.2f} | Size: {size:.6f} BTC | Fee: ${fee:.2f}"
    )

def check_trade_exit(current_high, current_low):
    global open_trade, wins, losses, total_pnl, total_fees

    if not open_trade:
        return

    entry = open_trade["entry"]
    stop  = open_trade["stop"]
    tp    = open_trade["tp"]
    size  = open_trade["size"]
    fee_o = open_trade["fee_open"]

    hit_tp   = current_high >= tp
    hit_stop = current_low  <= stop

    if not (hit_tp or hit_stop):
        return

    exit_price = tp if hit_tp else stop
    fee_c      = size * exit_price * 0.0026
    gross_pnl  = (exit_price - entry) * size
    net_pnl    = gross_pnl - fee_o - fee_c

    total_fees += fee_o + fee_c
    total_pnl  += net_pnl

    if hit_tp:
        wins += 1
        log.info(
            f"[{'PAPER' if PAPER else 'LIVE'}] TP HIT ✅ | "
            f"Exit: ${exit_price:,.2f} | Net P&L: ${net_pnl:+,.2f}"
        )
    else:
        losses += 1
        log.info(
            f"[{'PAPER' if PAPER else 'LIVE'}] STOP HIT ❌ | "
            f"Exit: ${exit_price:,.2f} | Net P&L: ${net_pnl:+,.2f}"
        )

    open_trade = None

# ── Status Print ──────────────────────────────────────────────────────────────
def print_status(price, indicators):
    total_trades = wins + losses
    win_rate     = (wins / total_trades * 100) if total_trades > 0 else 0.0

    log.info(
        f"[{'PAPER' if PAPER else 'LIVE'}] "
        f"BTC: ${price:,.2f} | "
        f"50MA: ${indicators.get('ma', 0):,.2f} | "
        f"RSI: {indicators.get('rsi', 0):.1f} | "
        f"Oversold(<{RSI_THRESHOLD}): {indicators.get('oversold', False)} | "
        f"Above50MA: {indicators.get('above_ma', False)} | "
        f"VWAP{'↑' if indicators.get('vwap_up') else '↓'}: "
        f"{'True' if indicators.get('vwap_up') else 'False'} | "
        f"GreenDot: {indicators.get('green_dot', False)}"
    )
    log.info(
        f"Trades: {total_trades} | Wins: {wins} | Losses: {losses} | "
        f"Win Rate: {win_rate:.1f}% | P&L: ${total_pnl:+,.2f} | "
        f"Fees: ${total_fees:,.2f}"
    )

# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    global tick_count, cooldown
    log.info("=" * 60)
    log.info("Cipher Bot v5.1 — Starting up")
    log.info(f"Pair: {PAIR} | Timeframe: {TIMEFRAME}min | Mode: {'PAPER' if PAPER else 'LIVE'}")
    log.info(f"MA Filter: 50MA | RSI Threshold: <{RSI_THRESHOLD} | Momentum: 0.05% of price")
    log.info(f"Capital: ${CAPITAL:,.2f} | Risk: {RISK_PCT*100}% | TP: {TP_MULTI}x")
    log.info("=" * 60)

    while True:
        try:
            if tick_count % 10 == 0:
                fresh = get_candles()
                if fresh:
                    candles.clear()
                    candles.extend(fresh)

            price = get_price()
            if price and len(candles) >= CANDLES_NEEDED:
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
