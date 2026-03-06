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
