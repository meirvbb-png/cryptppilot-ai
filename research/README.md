# Strategy research (honest backtests)
Data: MEXC spot public klines (`fetch.mjs`, needs BOTH startTime+endTime). Cost model: taker 0.055%/side + 0.02% slippage/side,
entry at NEXT bar open, SL-before-TP on same candle, 36-bar time stop. Params chosen on first 60% only, reported on last 40%.

Result (2026-11, 6 pairs): no strategy has a stable positive edge after costs.
- 5m: all 4 strategies negative (-0.3R..-0.5R per trade) – costs > stop distance. Even with maker-like costs still negative.
- 15m: Trend Pullback test +0.11R but train -0.27R (regime-dependent); others negative.
- 1h/365d: all negative.
Run: `node fetch.mjs <dir>` then `node backtest.mjs <dir> 15m`, `FEE=0.0002 SLIP=0.0001 node backtest.mjs <dir> 5m`.
