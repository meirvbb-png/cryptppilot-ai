# Stage 1 — Strategy research on real Bybit data

## Data (all real Bybit, no synthetic data)
- **Ticks**: every trade on BTCUSDT / ETHUSDT / SOLUSDT perpetuals, 2025-01-01 → 2026-11-23
  (692 days × 3) from `public.bybit.com/trading`, aggregated to 1-minute bars with taker buy/sell split.
- **Order book**: Bybit L2 200-level archives (`quote-saver.bycsi.com`, 100 ms), 7 days per coin
  (2026-11-17 → 23), rebuilt locally + all trades of the same days.

## Test protocol (strict)
- Signal on a closed bar → execution at the next 1-minute open. Every trade walked minute by minute.
  SL and TP in the same minute → SL assumed.
- Bybit non-VIP fees: taker 0.055 %, maker 0.020 %; 2 bps slippage on market / stop fills; funding
  0.01 % / 8 h charged on every position. Limit orders fill only when price trades *through* them.
- Every trade has a fixed Entry / Stop-Loss / Take-Profit at entry. Risk 1 % of equity per trade,
  max 5× notional, $500 starting equity.
- **Walk-forward**: parameters chosen on 2025 only (in-sample); the **same** parameters on all 3 coins;
  reported on 2026 (out-of-sample, a bear market: BTC 88k → 61k with a +39 % rally in between).
- ML: FreqAI-style sliding-window LightGBM (90-day train, weekly retrain, purged labels, DI outlier
  filter); predictions are always out-of-sample.
- Order-book strategies: first 4 days select, last 3 days test, $2 500 notional.

## Candidates tested
| Family | Source | Candidates | Param sets |
|---|---|---|---|
| Hummingbot directional | `bollinger_v1`, `macd_bb_v1`, `supertrend_v1`, + maker-entry variants | 4 | 232 |
| Hummingbot microstructure | OBI maker, OBI taker, `pmm_dynamic`-style quoting | 3 | 648 |
| Hummingbot `stat_arb` | ETH/BTC z-score pair | 1 | 32 |
| FreqAI | LightGBM regressor (`&-s_close`), LightGBM probabilistic triple-barrier classifier | 2 | 14 |
| Classic / order-flow | Donchian TSMOM, EMA-cross TSMOM, RSI-2, trend pullback, taker-flow momentum | 6 | 208 |

## Results — one parameter set per strategy, chosen on 2025, reported on 2026
| Strategy | TF | 2025 Sharpe / PF | **2026 net** (avg/coin) | 2026 Sharpe | 2026 PF | 2026 max DD | Trades 2026 | Per coin 2026 | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| Donchian TSMOM (n=55, SL 2 ATR, TP 10 ATR, EMA200) | 4h | 0.74 / 1.34 | **+9.2 %** | 1.05 | 1.50 | −13.9 % | 95 | BTC +13.9, ETH +2.4, SOL +11.2 | ✅ passes |
| EMA-Cross TSMOM (10/100, SL 2, TP 6) | 4h | 1.14 / 1.50 | +3.0 % | 0.19 | 1.07 | −13.1 % | 71 | BTC +8.3, ETH −0.5, SOL +1.1 | ⚠️ marginal |
| Order-flow momentum (taker imbalance) | 1h | −0.11 / 0.96 | +6.4 % | 0.43 | 1.07 | −14.3 % | 312 | BTC −2.7, ETH +17.4, SOL +4.3 | ❌ lost in 2025 |
| FreqAI LightGBM classifier | 4h* | −0.7 / 0.79 | −0.5 % | 0.02 | 0.99 | −15.7 % | 131 | — | ❌ |
| HB StatArb ETH/BTC | 1h | −0.90 / 0.71 | −3.3 % | −0.60 | 0.80 | −6.8 % | 39 | — | ❌ |
| HB SuperTrend | 4h | 1.21 / 1.81 | −4.7 % | −1.26 | 0.38 | −7.9 % | 107 | all 3 negative | ❌ overfit |
| HB MACD-BB | 15m | 0.84 / 1.37 | −8.6 % | −1.68 | 0.53 | −13.3 % | 104 | all 3 negative | ❌ overfit |
| FreqAI LightGBM regressor | 1h | 0.26 / 1.05 | −13.7 % | −1.22 | 0.75 | −27.9 % | 273 | all 3 negative | ❌ |
| HB Bollinger (taker / maker entry) | 1h | <0 | −26.8 % / −23.7 % | ≈ −1.9 | ≈ 0.75 | ≈ −37 % | 600–770 | all negative | ❌ |
| RSI-2 mean reversion (taker / maker) | 1h | <0 | −8.6 % / −4.9 % | < −1.5 | ≈ 0.6–0.7 | | | | ❌ |
| OBI maker / OBI taker / PMM dynamic | tick | — | **0 of 216 param sets net-positive on BTC; 0 on ETH** | | | | | SOL: a few positive on ~18 trades, same params lose on BTC/ETH | ❌ fees > edge |

\* best FreqAI configuration by 2025 data; the 1h classifier lost −34 % in 2026.

## Key findings
1. **Fees decide everything at retail tier.** Order-book strategies have some gross edge (64–95 of 216
   param sets gross-positive), but 4–7.5 bps round-trip fees + adverse selection wipe it out. BTC spread is
   1 tick (0.013 bps) — there is no spread to capture without a maker rebate (Bybit VIP / MM program).
2. **Short-term mean reversion (Bollinger, RSI-2, MACD-BB) loses on every coin** in 2026, even with maker entries.
3. **Overfitting is visible**: SuperTrend and MACD-BB had the best 2025 numbers and lost in 2026.
4. **Only slow trend-following survived** (4h Donchian robustly; 75 % of its 32 param sets were positive in 2026).
5. FreqAI-style ML did not beat fees on 1h; on 4h it was unstable (negative 2025, positive 2026 for some thresholds).

## Bottom line
Only **one** strategy passes the strict bar (profitable in both periods, on all three coins, with real fees):
4h Donchian trend-following. A second (4h EMA-cross) is marginal and highly correlated with it.
**There are not four distinct, proven strategies in this data.**
