# M13 Equity Momentum A+ — execution and full-move plan

## Non-negotiable objective

- Target **1–3 A+ equity momentum scalps per session**.
- Hard maximum **3**, not 5, for the first forward phase.
- Zero trades remains valid when no setup reaches A+ quality; never manufacture a minimum trade.
- Prioritize net accuracy, then capture the residual trend with a runner.

## Only entry archetype

The frozen entry remains:

**S1 Morning Base + an eligible real master setup variant + opening market breadth aligned >=55% + previous-session India VIX return >-2%.**

Eligible variants are not limited to NORMAL BUY/NORMAL SELL. They include BUY-EX, BUY-EX17, BUY-EX4–13, SELL-EX1–19, NORMAL BUY and NORMAL SELL. Only preview codes 90/290 are excluded. Direction and variant are stored separately.

Eligible window: 09:45–12:00 IST. Immediate signal-bar decision; no delayed/backfilled fill.

## How to reduce dozens of S1 signals to 1–3

The 19 one-year trades uniquely matched to the current engine were compared with 1,468 other same-day S1 candidates. The selected trades showed:

- median same-clock relative volume **3.97** versus **1.88** for other candidates;
- median side-aligned move from previous close **1.56%** versus **0.78%**;
- median master total score **95** versus **65**;
- median signal time **10:10** versus **10:20**;
- S1+S3 appeared disproportionately often among selected candidates.

These are selection/ranking traits, not separately proven outcome gates.

### A+ candidate score

Rank candidates closing on the same five-minute bar with the following weights:

1. **30% same-clock relative volume** — strongest selection discriminator.
2. **25% master signal strength/total score**.
3. **20% directional momentum/reversion profile** — previous-close displacement, EMA/VWAP extension and pullback/reentry context. These classify and rank the setup; they never veto an otherwise-valid M13 entry.
4. **10% video confluence** — S1+S3 first, then S1+S2, then S1 alone.
5. **10% side-aligned sector breadth**.
6. **5% liquidity, spread and achievable fill quality**.

Signal-candle close location and candle range are execution-quality vetoes, not large score components.

### Immediate allocation

- Evaluate and rank only signals from the same just-closed bar.
- Enter the top candidate immediately when it exceeds the frozen A+ score threshold.
- Maximum one new entry per five-minute bar.
- Maximum one symbol and one sector per day.
- Stop after three entries, two full-risk losses, or the daily loss limit.
- A later signal cannot be known in advance; never delay a valid entry merely to compare it with future candidates.

## Entry and stop

### Entry

- Marketable-limit order immediately after signal close.
- Reject if available fill is >0.10% beyond signal close or >0.25R worse.

### Initial SL

- BUY: `signal candle low × (1 - 0.0002)`.
- SELL: `signal candle high × (1 + 0.0002)`.

Reject candles whose stop is economically too narrow for spread/costs or too wide relative to ATR and the rupee-risk cap.

## Risk and 5x MIS sizing

Use risk-first sizing:

`qty_risk = floor(rupee_risk_budget / (abs(fill-SL) + adverse_slippage_per_share + cost_per_share))`

`qty_notional = floor(maximum_notional / fill)`

`quantity = min(qty_risk, qty_notional, liquidity_cap)`

Starting forward-test settings:

- planned risk: ₹750–₹1,000/trade;
- maximum two concurrent positions;
- maximum daily loss: 2R;
- allocated cash margin may be ₹50k–₹100k, but 5x leverage never overrides the risk-based quantity;
- no averaging down and no add-on to a losing trade.

## Exit plan: accuracy plus full-move runner

A fixed full-position target would improve headline accuracy but truncate the move. A full-position loose trail would capture more trend but give back too many wins. Use a two-leg hybrid.

### Phase 1 — prove momentum quickly

- Initial stop remains the signal-candle stop.
- If no new directional extreme occurs within two bars, exit.
- If MFE remains below +0.35R after three bars (15 minutes), exit at market.
- Exit early on a five-minute close beyond the signal-candle midpoint against the trade when live breadth is deteriorating.

### Phase 2 — lock the scalp

When price first reaches **+0.75R**:

- book **60%** of quantity;
- move the remaining 40% stop to entry plus estimated round-trip costs;
- the trade can no longer become a meaningful net loss under normal fills.

This level and fraction are initial research settings and must be compared with +1R/50% and +1R/60% alternatives chronologically.

### Phase 3 — capture the full move

For the remaining 40%:

- at +1.5R, lock at least +0.75R on the runner;
- trail using the tighter directional protection of:
  - confirmed two-bar swing low/high with 0.02% buffer; or
  - five-minute EMA9 with buffer;
- never loosen the stop;
- exit if live market breadth flips into the opposite regime;
- exit after two consecutive closes against the directional EMA structure;
- if the trade remains strong, allow the runner up to 60 minutes rather than imposing an early fixed target.

No new position should be opened merely because another trade exited.

## Shadow exit arms

Every A+ entry must be counterfactually evaluated under identical fills/costs:

- E1: full exit +0.75R;
- E2: full exit +1R;
- E3: 60% at +0.75R + 40% runner (primary);
- E4: 50% at +1R + 50% runner;
- E5: full-position EMA9/two-bar runner;
- E6: primary exit without breadth-flip exit.

Select the final exit using discovery and validation blocks. Do not choose it from full-sample or holdout results.

## Forward telemetry and arms

Log every master+S1 candidate, including rejected ones:

- Arm A: master+S1;
- Arm B: Arm A + opening breadth >=55%;
- Arm C: Arm B + prior VIX >-2% (M13 A+);
- actual available fill, signal candle, stop, MFE/MAE at 5/10/15/30/60 minutes;
- all six exit-arm outcomes after equity costs/slippage.

Only Arm C creates the primary paper trade.

Within Arm C, report all outcomes separately for:

- MOMENTUM-CONTROLLED;
- MOMENTUM-EXTENDED;
- PULLBACK-REENTRY;
- REVERSION-ALIGNMENT.

This determines whether anti-chase/reversion context improves equity outcomes without blocking any entry in advance.

## Three-bot alerts and strict no-repeat policy

Every M13 ENTRY, partial book, runner activation, stop/exit, EOD summary and Excel report fans out to:

1. main Telegram target;
2. M13 extra target A;
3. M13 extra target B.

Complete M13-specific A/B credential pairs are preferred, with complete M11 A/B pairs as fallback. Every deterministic alert key is appended to state and persisted **before** any bot call. A restart or queued duplicate cycle therefore sends nothing.

This is strict **at-most-once** delivery: avoiding duplicates has priority over retry. A crash after reservation but before one target receives the message can cause a missed alert; automatically retrying would violate the no-repeat requirement.

## Paper operation

M13 is isolated in `state13.json`, `data/m13_prev_context.json`, `learn/m13_candidates_DATE.csv` and `paper_test_M13_DATE.xlsx`. It never reads or writes M1–M12 state.

```bash
python test_m13_entry.py
python test_m13_trader.py
python test_m13_alerts.py
python m13_runner.py --test-alert
python m13_runner.py --loop 1
```

Workflow: **13. LIVE M13 — equity momentum A+ (max 3/day, paper-only)**.

## Acceptance criteria before capital

- at least 20 untouched sessions and preferably 50–60 completed equity trades;
- 1–3 entries/day, hard maximum 3;
- primary net win-rate target **>=85% after all equity costs/slippage**;
- at least 52 wins from the first 60 completed forward trades (86.7%) before considering capital;
- 95% Wilson lower bound preferably >=75%;
- profit factor >=2.0;
- positive expectancy after equity costs/slippage;
- no stale, delayed or backfilled fills;
- stable BUY and SELL results;
- runner adds net expectancy compared with the fixed-target arms.
