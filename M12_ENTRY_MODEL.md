# M12 Selective Reversion — standalone entry model

**Status:** paper-only research model  
**Universe:** the repository's 210 `fno_universe.csv` stocks  
**Instrument currently simulated:** NSE equity underlying (not a futures contract)  
**Hard limit:** at most **5 entries per trading day**; zero trades is valid

M12 is new and separate. It does not alter M1–M11, `master_scanner.py`, `trader.py`, or their states.

## Entry thesis

The cross-model interaction audit tested 30 causal gates and thousands of 1–4 gate combinations, followed by focused higher-order stability checks. The most stable immediate-entry pattern combined rules from different models: a corrected reliable signal family, strict anti-chase versus previous close, a sector that is counter/neutral rather than already crowded in the signal direction, at least one of M11's four video setups, and same-clock relative-volume confirmation.

M12 therefore rejects momentum chasing and sector crowding, while requiring a concrete price-action setup. It is deliberately selective rather than trying to fill five slots.

## Frozen hard rules

A candidate must satisfy every rule below using data available at the signal-bar close.

1. It must be a real chart signal from the current `master_scanner.py`; 90/290 preview rows are never candidates.
2. Signal time must be 09:45–12:00 IST so video setup detection is complete and causal.
3. Signal code must be in the current-engine whitelist:
   - 102 `BUY-EX17`
   - 201 `SELL-EX1`
   - 202 `SELL-EX2`
   - 209 `SELL-EX8`
   - 213 `SELL-EX12`
   - 216 `SELL-EX15`
   - 220 `SELL-EX19`
   - 280 `NORMAL SELL`
4. Define `s = +1` for BUY and `-1` for SELL. The side-normalized displacement from the immediately preceding trading session's close must be:

   `-1.00% <= s × (signal_close / previous_close − 1) × 100 <= +0.20%`

5. Side-normalized sector breadth versus previous close must be at most 0.43. In other words, no more than 43% of sufficiently fed sector members may already be moving in the proposed direction.
6. At least one video setup must be true on the signal bar:
   - S1 Morning Base;
   - S2 Pivot Pullback, using the correct prior-session daily pivot;
   - S3 Flag Breakout; or
   - S4 Sandwich.
7. Signal-bar volume must be at least the median volume of the preceding 20 sessions at the same clock time (`clock_relvol >= 1.0`).
8. Missing/stale previous closes, insufficient sector breadth/history, code/name mismatch, or side/code mismatch all **fail closed**.
9. A signal detected more than five minutes after its bar closes is rejected; M12 never backfills an entry at an old paper price.

EMA structure, broad-market breadth, mover rank, stock-versus-sector leadership, sector-versus-market leadership and OI rank remain timestamped telemetry. They are not hidden vetoes: aligned market/sector leadership did not show stable standalone improvement.

The previous close is not read blindly from stale history. `data/m12_prev_close.json` is seeded after each session. A cache/history date must equal the prior weekday and contain completed 15:20-or-later bars for at least 180 stocks. If freshness cannot be proved—including the first session after an exchange holiday—M12 takes no entries and seeds the next-day cache at EOD.

## Ranking and portfolio constraints

M12 ranks only simultaneous qualified candidates. The transparent quality score rewards:

- less directional displacement;
- optional EMA anti-extension telemetry;
- closeness to EMA20;
- non-extended opening gap;
- number of aligned video setups; and
- sector breadth measured at the candidate timestamp.

The score is not a claimed probability.

Portfolio constraints:

- the decision is made immediately when the signal bar closes;
- no batching, leaderboard wait, late confirmation, or delayed entry is intentional;
- maximum 5 entries/day is a hard safety ceiling, not a quota;
- maximum 3 entries on one side;
- maximum 1 entry per symbol/day;
- maximum 1 entry per sector/day.

Every accepted setup has already passed the complete A+ checklist. In the 44-session replay, the stricter rules produced no more than three entries on any day. A live system cannot know whether a better signal will appear later, so if the hard ceiling is ever reached, later signals cannot be taken without using delayed/look-ahead selection.

## Exits, sizing, and costs

M12 calls the current `trader.evaluate()` unchanged:

- setup-aware structure SL;
- ₹50,000 maximum notional and ₹900 planned-risk sizing cap;
- no fixed target;
- current +1R trail-arm behavior;
- 15:20 square-off.

Reports call the current `costs.py` model. These are simulated fills and charges, not an execution guarantee.

## Historical evidence

Current-engine replay on 210 stocks and 44 common sessions (29-May–30-Jul-2026), after modeled costs and slippage:

| Chronological block | Sessions | Trades | Trades/day | Net wins | Win rate | Net P&L | Expectancy |
|---|---:|---:|---:|---:|---:|---:|---:|
| Train | 25 | 34 | 1.36 | 31 | 91.18% | ₹10,537 | ₹310/trade |
| Validation | 9 | 14 | 1.56 | 12 | 85.71% | ₹4,165 | ₹298/trade |
| Retrospective holdout | 10 | 13 | 1.30 | 11 | 84.62% | ₹2,871 | ₹221/trade |
| Combined | 44 | 61 | 1.39 | 54 | 88.52% | ₹17,573 | ₹288/trade |

The combined 95% Wilson lower bound is 78.16%; validation is 60.06% and holdout is 57.76%. This is **not** a forecast. The dataset covers only 44 sessions, uses the current universe retrospectively, and was subject to repeated strategy research. Treat the result as a hypothesis requiring new forward evidence.

The 03–07 Aug report files cannot serve as a clean external validation: committed history stops on 30-Jul, prior-close-derived learning fields can therefore be stale, and many reports were finalized with positions still marked `OPEN`.

## Required forward acceptance gate

Keep M12 paper-only until all conditions hold on **new, untouched sessions**:

- at least 20 sessions and preferably at least 60 closed trades;
- no day exceeds 5 entries;
- no duplicate decision IDs or duplicate executed rows;
- 100% of reports have actual 15:20/SL/trail exits (no EOD `OPEN` marks);
- net win rate at least 70%;
- 95% Wilson lower bound at least 60%;
- positive net expectancy and profit factor at least 1.5;
- results remain positive after a harsher slippage stress test.

If these conditions fail, do not loosen thresholds using the same forward sample. Diagnose, freeze a new version, and start a new untouched test.

## Files

- `m12_entry.py` — pure entry rules, causal features, ranking helpers
- `m12_runner.py` — separate live paper runner/state/report path
- `test_m12_entry.py` — code-map, threshold, causality, freshness, and cap tests
- `.github/workflows/12_live_m12.yml` — independent paper workflow
- `analysis/evaluate_m12.py` — chronological replay evaluator
- `analysis/gate_interaction_audit.py` — cross-model gate and winner-commonality audit
- `analysis/output/gate_combinations.csv` — retained causal interactions
- `analysis/output/m12_evaluation.json` — reproducible metrics
- `analysis/DEEP_AUDIT.md` — full repository/report audit

## Three-bot alerts

M12 fans every ENTRY, management/exit event, EOD summary and Excel report to:

1. main `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`;
2. extra target A;
3. extra target B.

Complete `M12_BOT_TOKEN_A`/`M12_CHAT_ID_A` and B pairs are preferred. If absent, the corresponding complete M11 A/B pairs are reused.

Every deterministic alert key is persisted **before** any bot call. A restart or duplicate queued cycle therefore sends nothing. This is strict at-most-once delivery: no duplicate has priority over retry, so a crash after reservation can cause a missed target rather than a repeated alert.

Test without touching trading state:

```bash
python m12_runner.py --test-alert
```

## Operation

```bash
python test_m12_entry.py
python analysis/evaluate_m12.py
python m12_runner.py --loop 1
```

The first live session may intentionally take no trades when the previous-close cache is stale. It will seed `data/m12_prev_close.json` after the close for the next session.

The workflow requests a five-minute cadence, but GitHub Actions can queue jobs. M12 rejects a signal once it is over five minutes old rather than pretending it filled at the historical close. For genuinely prompt alerts, run the model on an always-on VM with a reliable five-minute scheduler; no cloud cron can guarantee zero latency.

To stop M12, disable workflow **12. LIVE M12**. Existing models are unaffected.
