# ⬡ Cipher Bot — Market Cipher Strategy Engine

A professional crypto trading bot running Market Cipher-inspired signals on Kraken Pro.

## Strategy Rules
- Signal: Green Dot + VWAP Above + Clear Trend (all 3 required)
- Entry: Next candle open after signal confirmation
- Stop Loss: Below signal candle low
- Take Profit: 2x risk distance
- Position Size: 2% of capital per trade
- Cooldown: 5 candles after loss, 3 after win
- Fee Rate: 0.1% per side (Kraken Pro)

## Deployment on Railway

1. Create a new project on Railway
2. Deploy from GitHub or upload these files
3. Add environment variables (see .env.example)
4. Deploy — bot runs 24/7 automatically

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| KRAKEN_API_KEY | Your Kraken API key | required |
| KRAKEN_API_SECRET | Your Kraken private key | required |
| TRADING_PAIR | Kraken pair symbol | XBTUSD |
| TIMEFRAME_MINUTES | Candle timeframe in minutes | 240 (4h) |
| CAPITAL | Paper trading capital | 10000 |
| RISK_PCT | Risk per trade percentage | 2 |
| TP_MULTI | Take profit multiplier | 2 |
| PAPER_MODE | Paper trading mode | true |

## Safety
- Never enable withdrawals on your Kraken API key
- Keep PAPER_MODE=true until strategy is proven profitable
- Monitor the Railway logs regularly

- ## Development Roadmap

### Stage 1 — Foundation (March-April 2026) ✅ IN PROGRESS
- 60 minute candles, RSI 38, current momentum thresholds
- Target: Evaluate signal quality with real trades
- Checkpoint: Mid-April 2026
- Current: 1 trade, 1 win, $499.60 P&L

### Stage 2 — Threshold Optimization (April-June 2026)
- Adjust RSI to 42, lower momentum threshold slightly
- Add VWAP below confirmation filter
- Target: 4-6 trades/month, 60-65% win rate
- Checkpoint: End of June 2026

### Stage 3 — Short Capability (June-September 2026)
- Add short selling on Red Dot signals
- Doubles opportunity set without changing core logic
- Target: 6-8 trades/month
- Checkpoint: End of September 2026

### Stage 4 — Multi-Timeframe Confirmation (Sept-Dec 2026)
- Add 4-hour chart confirmation before firing signals
- Improves signal quality and win rate
- Target: 65-70% win rate
- Checkpoint: End of December 2026

### Stage 5 — Dynamic Thresholds (2027)
- Volatility-based RSI threshold adjustment
- Self-adapting to market conditions
- Target: 7-10 trades/month, 70-75% win rate
- Ultimate goal checkpoint: Mid 2027

### Long Term Performance Target
- 7-10 trades/month
- 75% win rate
- Monthly return: 8-15% in favorable conditions
- Monthly return: 0-5% in range bound conditions
