# M13 Equity Momentum A+ — single frozen entry setup

## Objective

Catch 1–3 immediate F&O-stock equity momentum scalps per day (hard maximum 3), using only one entry archetype. Do not fill a quota.

## The only entry archetype

**S1 Morning Base + real master momentum signal + breadth-aligned market + no prior VIX crush.**

Every mandatory condition below must pass at the five-minute signal-bar close.

## Mandatory rules

### 1. Instrument and time

- Current liquid NSE F&O stock; equity MIS execution.
- Signal-bar time from **09:45 through 12:00 IST**.
- Signal must be detected within five minutes of bar close. Never backfill an old price.

### 2. S1 Morning Base

- BUY: the session low through the signal bar must still be the low formed during the first 30 minutes; no lower low after 09:45.
- SELL: the session high through the signal bar must still be the first-30-minute high; no higher high after 09:45.

S1 is the only mandatory video setup. S2/S3/S4 may increase rank but cannot create a trade without S1.

### 3. Real master setup variant

`BUY` and `SELL` describe direction only; they do **not** mean that only NORMAL BUY/NORMAL SELL may trade.

All enabled real chart variants can qualify, including:

- BUY: NORMAL BUY (80), BUY-EX (101), BUY-EX17 (102), BUY-EX4/5/6/7/8/9/10/11/12/13 (103–112).
- SELL: SELL-EX1 through SELL-EX19 (201–220, including SELL-EX5S), and NORMAL SELL (280).

Codes 90/290 (`ENTRY BUY`/`ENTRY SELL`) remain blocked because they are scanner-table previews rather than real chart labels.

A variant never creates a trade by itself. It must also pass S1, market breadth, prior-VIX, execution and A+ quality rules.

M12-style anti-chase and reversion logic is still calculated, but it is **not an entry veto** for M13. The verified options ledger describes momentum continuation, including trades already extended from the previous close. Anti-chase/reversion fields are used to classify and rank candidates and to discover which equity subtype performs best.

### 4. Opening market breadth alignment

Using the liquid F&O universe at the open:

- BUY: at least **55%** opened above the immediately preceding close.
- SELL: at least **55%** opened below the immediately preceding close (opening advances <=45%).

At signal time, live breadth must not have flipped to the opposite regime. Opening breadth is the proven gate; live breadth is a safety check and telemetry until forward evidence accumulates.

### 5. VIX regime

- Immediately preceding session India VIX return must be **greater than -2.0%**.
- If VIX fell by 2% or more in the prior session, take no M13 trade that day.

### 6. Signal candle and immediate fill

- Enter only at/just after the signal-bar close using a marketable-limit order.
- BUY requires a bullish signal candle and SELL requires a bearish signal candle, as confirmed by the master signal.
- Reject if the available fill is more than 0.10% beyond the signal close or more than 0.25R worse than the intended entry.

### 7. Initial stop

- BUY: `signal candle low × (1 - 0.0002)`.
- SELL: `signal candle high × (1 + 0.0002)`.
- Quantity is risk-based and capped by allowed notional/margin; leverage never increases the fixed rupee-risk budget.

## Selection when several signals close together

Rank only candidates from the same completed bar, using:

1. higher margin above the 55% market-breadth threshold;
2. stronger same-direction sector opening breadth;
3. higher same-clock relative volume;
4. S1+S3, then S1+S2, then S1 alone;
5. better liquidity and smaller execution slippage;
6. stronger signal-candle close location.

Enter immediately after ranking that bar. Never wait for later candidates.

## Portfolio controls

- Normal target: 1–3 trades/day.
- Hard maximum: 3.
- One trade per symbol/day.
- Initially one trade per sector/day.
- Maximum 2 concurrent positions until forward testing supports more.
- Stop new entries after two full-risk losses or the daily loss limit.

## Gates deliberately not mandatory

- OI rank: top-10 result is promising but based on only 25 labeled candidates across 3 days.
- F&O top-20 mover: no stable edge in the reconstructed study.
- Stock leading its sector or sector leading the market: no stable standalone lift.
- M12 anti-chase/EMA reversion conditions as hard blockers: they remain ranking and shadow-analysis tags because the one-year momentum trades include both controlled and extended entries.
- S2/S3/S4 alone: insufficient one-year tagged evidence.

All remain shadow telemetry for future evaluation.

## Anti-chase and reversion overlay — ranking only

Every otherwise-valid M13 candidate receives the following causal tags:

- side-normalized move from previous close;
- opening gap and distance from the S1 morning base;
- distance from EMA9, EMA20, EMA50 and VWAP in ATR units;
- candle range/ATR and close location;
- stock/sector/market crowding;
- breakout, pullback, continuation or reversal classification;
- actual fill distance from the master signal close.

The candidate is assigned to one research subtype:

1. **MOMENTUM-CONTROLLED** — directional expansion with strong volume, not extremely stretched.
2. **MOMENTUM-EXTENDED** — strong valid momentum but far from previous close/EMA/VWAP.
3. **PULLBACK-REENTRY** — momentum resumes after an EMA/VWAP/pivot retest.
4. **REVERSION-ALIGNMENT** — M12-like anti-chase/reversion context that also has the M13 master+S1 setup.

These tags cannot reject a valid M13 setup. They affect ranking only when several candidates close together, and all four subtypes are shadow-evaluated separately.

A small ranking bonus may be given to controlled momentum or clean pullback/reentry. Extreme extension receives no bonus, but it remains eligible. The only hard chase rejection is an execution problem—an actual fill more than 0.10% or 0.25R beyond the signal close—not a chart-classification veto.

## Master-variant evidence recoverable from the overlap

Only 19 one-year trades could be uniquely mapped to the current scanner version, so variant statistics are ranking evidence—not a safe hard whitelist:

- BUY-EX: 6/7 wins.
- BUY-EX7: 2/3 wins.
- BUY-EX17: 2/2 wins.
- SELL-EX1: 2/2 wins.
- BUY-EX12, BUY-EX5, BUY-EX4, NORMAL SELL and SELL-EX19: 1/1 each.

Because the old ledger does not record master names and scanner versions changed, excluding every unobserved variant would be false precision. Log performance by current numeric code during forward testing and recalibrate only after adequate support.

## Evidence from the 221-record verified-P&L ledger

Base known outcomes:

- 217 known trades, 199 wins, 18 losses: **91.71%**.

Context gate — market opening breadth aligned >=55% and prior VIX return >-2%:

- 90 known outcomes: 89 wins, 1 loss: **98.89%**.
- 95% Wilson lower bound: **93.97%**.
- 2 additional unknown-P&L rows pass; counting both as losses gives **96.74%**.
- Chronological blocks: 98.36%, 100%, 100%.

This is retrospective conditional evidence from taken trades, not a guaranteed live equity win rate. Equity forward testing is mandatory because the source outcomes were options trades.

## Forward acceptance

Keep paper-only until at least 20 sessions and preferably 50–60 closed equity trades. Require:

- net win rate target **>=85% after full equity costs and slippage**;
- at least 52 wins in the first 60 completed forward trades before capital is considered;
- Wilson lower bound preferably >=75%;
- positive expectancy after full equity costs and slippage;
- profit factor >=2.0;
- no stale/backfilled entries;
- no more than three trades/day;
- stable BUY and SELL performance.

## Runnable paper model

Files:

- `m13_entry.py` — hard A+ gates, scoring and subtype tags.
- `m13_trader.py` — signal-candle stop, 60% scalp book and 40% runner.
- `m13_runner.py` — feeds, scanner, state, selection, management, EOD report and candidate labels.
- `m13_alerts.py` — three-bot at-most-once fanout.
- `test_m13_entry.py`, `test_m13_trader.py`, `test_m13_alerts.py`.
- `.github/workflows/13_live_m13.yml` — independent five-minute paper workflow.

Commands:

```bash
python test_m13_entry.py
python test_m13_trader.py
python test_m13_alerts.py
python m13_runner.py --test-alert
python m13_runner.py --loop 1
```

The first session can intentionally take no trades when its own previous-close cache is stale; it seeds `data/m13_prev_context.json` after the close.
