# 1m scan audit — 2026-11-25: why the big winners were missed, and the edge

**Bottom line:** the scanner was not blind. The board had the eventual leaders, often ranked #1 all session. It missed them because (1) the live pick was effectively a **single, same-side sector book** locked to FMCG, (2) the entry path kept old fire levels and then refused names as `earlier`, `follower`, `no_target`, or `candle_cap`, and (3) it entered lower-board-rank names instead. Even the winners it did take were under-monetized: the EMA9 trail gave back **83.5% of MFE**, and costs were **55.8% of gross**.

The concrete edge supported by this tape is a **second book for rebased top-1/top-2 leaders at a sector's first high-conviction qualification**, independent of the main market-aligned pick, plus a stricter board-rank gate on ordinary entries. A causal, cost/stop-aware replay of that second book produces **+14.09R hold-to-close**, with honest trailing-exit bounds of **+0.59R to +5.04R** on six trades. That is one tape, not a track record.

---

## 1. Inputs and data integrity

The authoritative inputs are the raw exports:

- `inputs/audit-2026-11-25-1m.json` — 23 top-level keys; 72 candles, 993 leadership rows, 110 blocked signals, 70 effective rules, 40 `fresh_leaders` events.
- `inputs/board-2026-11-25-1m.html` — 17 tables, 190 name-sector rows, 72 five-minute bars, `data-v` values and `title` rank/position metadata.
- `inputs/audit-2026-11-25-1m.pdf` and `inputs/board-2026-11-25-1m.pdf` are print views; they were used only as cross-checks. Exact values below come from JSON/HTML.

The label says **“1m”**, but the board itself says: *“72 candle(s) · 15 sector(s) · 13679 name-scorings · every 5 min · scored on 1d of 5m data · scan 09:20–15:15.”* JSON confirms `board.meta.every_min = 5`, `period = "1d"`, `sweeps = 72`, `names = 13679`, `stale = 1`. So this is a bot/model named `1m` evaluated on a **5-minute close board**, not a true one-minute microstructure tape. Do not extrapolate the edge to 1-minute fills without 1m bar replay.

The parsed HTML produces 13,680 name-bar cells (`190 × 72`), with exactly **one missing `mv`** cell, matching `stale = 1`. It also contains **53 boxed pick cells**, matching `counts.judged = 53`, and every parsed `hit` top-5 marker matches `board.summary.top5`. These checks are in `metrics.json`.

Two side fields must not be confused:

- The **sector board** and JSON `sectors[*].side` show each sector's **final/end-of-day side**.
- The **name heatmap headings** and JSON `board.summary[*].side` show the sector's **09:15 side**.

Six sectors flipped during the day: `AUTO`, `CDUR`, `DEF`, `IT`, `PSE`, `REALTY`. All “directional last move” figures below use the sector's final side from JSON; all counterfactual entries use the **per-bar dominant bull/bear side** from `timeline`, not a stale static side.

There is no evidence in the export of a feed or Telegram-delivery failure: `diagnostics.feed_behind`, `scan_late`, `lag_*`, and `early_age_*` are blank, `alert_timing = []`, `undelivered = 0`, `secondary_failures = 0`, and `no_bar` is only `["15:15","15:20","15:25"]`. The miss is a decision-rule problem, not an obvious missing-data problem.

---

## 2. What the day actually produced

| Item | Value |
|---|---:|
| Market | `BULL`, confidence 65, regime `TRANSITION` |
| Last pick / reason | `FMCG`; *“PERSIST + FMCG qualified (score 97); also scanning PHA 77”* |
| Candles / candles with pick | 72 / 53 |
| Alerts / entries | 15 / 15 |
| Closed trades | 14: 5W / 9L, win rate 35.7% |
| Open trades | 1: HINDCOPPER MET SELL, +₹87.62 / +0.04R |
| Gross / costs / net | ₹2,489.48 / ₹1,389.23 / ₹1,100.27 |
| Costs as % of gross | **55.8%** |
| Closed net R | **+0.14R**; +0.18R including the open trade |
| PF / avg hold | 1.31 / 64 min |
| Avg MFE / give-back | 1.17R / 0.98R; **83.5% of MFE given back** |
| Exits | 10 EMA9 EXIT, 4 SL HIT |

The largest realized winners were VBL `+2.26R`, GODREJCP `+2.15R`, PFC `+1.43R`, BRITANNIA `+0.92R`, and BAJFINANCE `+0.44R`. But VBL, PFC, and GODREJCP gave back **1.64R, 1.88R, and 0.99R** from MFE respectively.

---

## 3. The big winners were visible and mostly refused

Define a **big winner** as a unique name with at least **+3.0 percentage points in its sector's final dominant direction** by 15:10. Where a name appears in two sectors (PFC, HAL, POWERGRID, HDFCBANK), keep its best sector row. This yields **15 names; 12 were not entered**. Nine of the 15 were final short-side winners; the three entered names were all long-side FMCG names.

| # | Name | Sector | Final side | Raw last % | Directional last % | Top5/72 | Best rank | Entered / net R | Final refusal |
|---:|---|---|---:|---:|---:|---:|---:|---|---|
| 1 | SUNTV | MEDIA | short | -8.59 | **+8.59** | 72 | 100.0 | no | `earlier` |
| 2 | NAZARA | MEDIA | short | -7.33 | **+7.33** | 69 | 99.8 | no | `earlier` |
| 3 | TIPSMUSIC | MEDIA | short | -6.53 | **+6.53** | 71 | 99.5 | no | `earlier` |
| 4 | DABUR | FMCG | long | +6.24 | **+6.24** | 71 | 99.9 | no | `earlier` |
| 5 | OFSS | IT | short | -5.65 | **+5.65** | 65 | 100.0 | no | none recorded |
| 6 | COFORGE | IT | short | -5.50 | **+5.50** | 65 | 99.8 | no | none recorded |
| 7 | HINDUNILVR | FMCG | long | +5.08 | **+5.08** | 66 | 99.9 | no | `follower` |
| 8 | GODREJCP | FMCG | long | +5.02 | **+5.02** | 60 | 99.9 | yes, +2.15R | — |
| 9 | KALYANKJIL | CDUR | short | -4.56 | **+4.56** | 69 | 99.5 | no | `earlier` |
| 10 | DIXON | CDUR | short | -4.21 | **+4.21** | 70 | 97.2 | no | `earlier` |
| 11 | BRITANNIA | FMCG | long | +3.64 | +3.64 | 29 | 78.5 | yes, +0.92R | — |
| 12 | ZEEL | MEDIA | short | -3.56 | +3.56 | 65 | 81.3 | no | `follower` |
| 13 | DIVISLAB | PHA | long | +3.26 | +3.26 | 58 | 99.9 | no | `earlier` |
| 14 | PERSISTENT | IT | short | -3.23 | +3.23 | 66 | 97.6 | no | none recorded |
| 15 | NESTLEIND | FMCG | long | +3.11 | +3.11 | 34 | 75.7 | yes, -0.06R | — |

Full rows are in `big_winners.csv`.

The `earlier` label is especially damning on this tape. There are **46 `earlier` blocked events** in the audit, but only **10 names end the board with `earlier` as their final refusal**. Those 10 all finished in the right direction:

| Name | Sector | Directional last % | Top5/72 | Best rank | Avg rank |
|---|---|---:|---:|---:|---:|
| SUNTV | MEDIA | +8.59 | 72 | 100.0 | 79.4 |
| NAZARA | MEDIA | +7.33 | 69 | 99.8 | 74.9 |
| TIPSMUSIC | MEDIA | +6.53 | 71 | 99.5 | 76.8 |
| DABUR | FMCG | +6.24 | 71 | 99.9 | 81.4 |
| KALYANKJIL | CDUR | +4.56 | 69 | 99.5 | 78.3 |
| DIXON | CDUR | +4.21 | 70 | 97.2 | 78.9 |
| DIVISLAB | PHA | +3.26 | 58 | 99.9 | 70.7 |
| ITC | FMCG | +2.76 | 55 | 87.6 | 59.2 |
| JINDALSTEL | MET | +2.72 | 71 | 86.6 | 65.3 |
| WHIRLPOOL | CDUR | +2.44 | 47 | 90.4 | 60.1 |

Mean directional move: **+4.86pp**; median **+4.38pp**; all were top-5 for 47–72 of 72 bars. These were persistent leaders, not one-bar spikes. This is hindsight on one session, so it is evidence for re-admission of *re-asserting leaders*, not proof that every `earlier` signal should be bought on every day.

---

## 4. Root cause 1 — one sector pick, aligned with the index, suppressed the other books

The timeline's pick distribution is:

| Pick | Candles |
|---|---:|
| FMCG | **50** |
| WAIT | 19 |
| MET | 2 |
| PHA | 1 |

So **50 of 53 non-WAIT candles (94.3%)** were FMCG. `PICK_STICKY_MARGIN = 5` and FMCG's persistent 95–99 score kept the pick there even when other sectors were one-way. At 15:10 the audit says `FMCG 97; also scanning PHA 77` — a 20-point margin.

The shared picker in this checkout is explicitly **complementary to index direction**: `nse_scanner/market.py::_complementary_pick()` chooses from `persist_plus` when `commit_side = 1` and `persist_minus` when `commit_side = -1`; `nse_scanner/pipeline.py` then scans only `md.pick_sector`. The audit runtime has extra knobs, but its effective `DIVERGENCE_SHORT = 0`, `SECTOR_FOLLOW_ON_WEAK = 0`, `SECTOR_FOLLOW_MAX = 2`, and `SECTOR_EXTRA_MAX = 1` are consistent with that architecture. `DUAL_PICK = 1` / `TRIPLE_PICK = 1` allow multiple names *inside the pick/extras*, not a genuinely independent second sector book.

Sector-level evidence:

| Sector | Final side | Day % | Gate candles | Pick candles | Entries |
|---|---:|---:|---:|---:|---:|
| AUTO | long | +1.04 | 0 | 0 | 0 |
| BANK | long | +1.70 | 13 | 0 | 0 |
| CDUR | short | -0.33 | 21 | 0 | 1 |
| DEF | long | +0.78 | 0 | 0 | 0 |
| ENE | short | -0.11 | 7 | 0 | 0 |
| FIN | long | +2.11 | 17 | 0 | 6 |
| FMCG | long | **+3.50** | 38 | **50** | 5 |
| IT | short | +0.87 | **0** | **0** | 0 |
| MEDIA | short | **-4.29** | **29** | **0** | 0 |
| MET | short | -2.27 | 24 | 2 | 2 |
| OIL | long | +0.79 | 9 | 0 | 0 |
| PHA | long | +2.09 | 7 | 1 | 1 |
| PSE | long | +0.84 | 3 | 0 | 0 |
| PSUBK | short | +0.22 | 5 | 0 | 0 |
| REALTY | short | 0.00 | 14 | 0 | 0 |

MEDIA had the largest absolute day move (-4.29%, intra -3.78%), bear persistence 97 by 09:35 and 100 by 10:20, **29 gate candles**, 192 leadership rows (24 bars with a top-5 scanned), and **zero picks / zero entries**. Its three biggest shorts — SUNTV, NAZARA, TIPSMUSIC — were the three biggest winners on the whole board.

IT's `day %` is +0.87 because it opened strong, but its **intra move was -1.13%** and the per-bar dominant side flipped to bear around 09:45. It never reached the gate (`max bear persist ≈ 65`, `gate_candles = 0`), had **zero leadership rows**, and produced no trades despite OFSS, COFORGE, and PERSISTENT ending among the top 15 directional winners. CDUR had 21 gate candles and no picks, yet KALYANKJIL and DIXON ended -4.56% and -4.21%.

`FIN` and `CDUR` did produce entries despite zero pick candles, consistent with the effective `SECTOR_FOLLOW = 1` / `SECTOR_FOLLOW_EXTRAS = 1` path rather than an independent sector book; the export does not identify the exact call path. The `SCREEN_BOT = 1` and `SCREEN_FAILOVER = 1` knobs in the effective config did not translate into a MEDIA or IT trade.

---

## 5. Root cause 2 — old fires, trim labels, and candle caps discarded re-asserting leaders

The audit has **110 blocked signals**:

| Rule | Count | Meaning in JSON |
|---|---:|---|
| `follower` | 59 | *“followers not scanned — outside the sector's top”* |
| `earlier` | 46 | *“fired before their sector was picked”* |
| `candle_cap` | 3 | *“not taken — the candle entry limit was reached”* |
| `no_target` | 2 | *“without a pivot target”* |

The board's final `refused` labels are 58 `follower`, 10 `earlier`, and 122 blank/entered. The final `earlier` names are exactly the persistent winners listed in section 3.

The late-leader machinery was **on**:

- `PICK_CATCHUP = 1`
- `LATE_LEADER_REBASE = 1`
- `LATE_LEADER_ENTRY = 1`
- `TARGET_FALLBACK = 1`
- `MIN_TARGET_R = 0.5`, `MIN_TARGET_PCT = 0.10`
- `SCAN5M_LEADER_CATCHUP = 1`, `SCAN5M_LEADER_MAX_MINUTES = 40`, `SCAN5M_LEADER_MAX_R = 2.0`
- `SCAN1M_EARLY_FROM = 09:30`

Yet `diagnostics.catchup_skip` is `{"chased": 38, "none": 2, "stop": 26, "old": 56}`. The `fresh_leaders` diagnostics show **40 events** (23 catchup, 9 confirm, 8 projection), including:

- `09:35|catchup|DABUR` — rank #1, `status = other`, sector persist delta +6, unentered; from that candle to close was **+3.69pp / +5.00R** under the same risk/cost model.
- `10:00|catchup|HINDUNILVR` — rank #3, `status = preferred`, score delta +24.9, sector persist delta +8, unentered; from that candle to close was **+3.99pp / +5.42R**.
- `09:55|catchup|BRITANNIA` and `13:35|catchup|GODREJCP` — both `preferred` and both entered, returning +3.76R and +2.28R hold-to-close in the same scoring.

So the problem is not “no rebase”. It is **which re-assertions are allowed to become a trade**, and the fact that they still have to pass through the main-pick/stale-fire/entry-cap path. Specific refusal examples, scored from the refusal candle to 15:10 with the median actual stop (0.718%) and median cost (0.140R):

| Refusal | Name | Directional move after refusal | Stop-aware hold net R |
|---|---|---:|---:|
| `no_target` 09:30 | TATACONSUM | +0.87pp | +1.07R |
| `no_target` 09:35 | DABUR | +3.69pp | **+5.00R** |
| `candle_cap` 09:55 | AXISBANK | +0.44pp | +0.47R |
| `candle_cap` 09:55 | ITC | +0.88pp | +1.09R |
| `candle_cap` 09:55 | HINDUNILVR | +3.78pp | **+5.13R** |

These overlap with the big-winner table; do not add them together. They show that the cost of a rigid “three entries per candle” and stale target logic was concentrated in the strongest names.

---

## 6. Root cause 3 — the entries actually taken were mostly lower-board-rank names

Using the HTML board rank/position on each entry candle (`entry_quality.csv`):

| Board position at entry | Trades | Net R | Avg net R |
|---|---:|---:|---:|
| #1–#2 | 3 | **+3.51R** | +1.17R |
| #3 | 4 | -1.14R | -0.285R |
| #4 or worse | 8 | -2.19R | -0.274R |
| All 15 incl. open | 15 | +0.18R | +0.012R |

The three top-2 entries were PFC (#1, +1.43R), WELCORP (#2, -0.07R), and GODREJCP (#1, +2.15R). The rest included BSE #11 (-1.25R), KOTAKBANK #12 (-1.50R), CHOLAFIN #15 (-1.20R), VBL #8 (+2.26R), BRITANNIA #6 (+0.92R), COLPAL #4 (-0.84R), TORNTPHARM #4 (-0.62R), and HINDCOPPER #4 (+0.04R open).

Effective config allowed a broad band: `SECTOR_TOP_N = 5`, `ENTRY_MIN_RANK = 40`, `ENTRY_RANK_BAND = 0.75`, `NAME_RANK = combined`, `TRIPLE_PICK = 1`, `SECTOR_FOLLOW = 1`, `SECTOR_FOLLOW_EXTRAS = 1`, `MAX_ENTRIES_PER_PICK = 0`. The exact runtime code is not in this checkout (section 9), so this should be read as an observed outcome, not a line-by-line code diagnosis: **the ledger bought the tail of the board while the leaders were refused.**

---

## 7. Root cause 4 — exits and costs turned good selection into a flat day

Even the 5 winners were under-monetized. Closed trades summed:

- MFE: **16.37R**
- Give-back: **13.67R**
- EMA9-exit give-back alone: **8.72R**
- Gross: **2.70R**
- Costs: **2.53R**
- Net: **+0.14R**

`BREAKEVEN_AFTER_T1 = 1`, `EXIT_EMA9_BUFFER_PCT = 0.10`, and `EXIT_EMA9_BUFFER_R = 0.25` were already effective, but VBL (2 targets hit), PFC (1), and GODREJCP (2) still surrendered 1.64R / 1.88R / 0.99R from MFE. A conservative **recovery of just 25% of EMA9 give-back** would add 2.18R gross, moving closed net from +0.14R to about **+2.35R** without a single new signal.

Costs are the second tax: ₹1,389.23 on ₹2,489.48 gross, or **55.8%**, with median cost **0.140R per trade**. Four SL hits had stop distances 0.201%–0.505% and cost/R 0.198–0.497; KOTAKBANK alone paid 0.497R of cost before losing 1.50R. Any new edge must be tested **after** this friction, not on raw percentage moves.

---

## 8. The edge: a rebased top-2 “second book” on first high-conviction qualification

### Rule (causal; uses only information available on the trigger candle)

1. At every 5m bar, compute each sector's **dominant persistence** and **counter persistence** from `timeline`: `max(persist_bull, persist_bear)` and `min(...)`.
2. Trigger a sector when it is `qualified`, dominant persistence **≥ 90**, and counter persistence **≤ 10**. This is stricter than the effective `SCREEN_PERSIST_MIN = 78` and is meant to isolate a one-way tape.
3. Take the sector's **top two board-position names** with composite rank **≥ 55** (`SCREEN_MIN_SCORE = 55`), on the sector's **current dominant side**, even if the main pick is a different sector or `WAIT`.
4. **Rebase** entry, stop, and targets to the trigger candle; do not use a fire from 09:20 at 10:10. This is the `LATE_LEADER_REBASE` / `TARGET_FALLBACK` idea applied to a **separate sector book**, not only to a late main-pick change.
5. Keep the existing effective caps: `SCREEN_MAX_DAY = 6`, `SCREEN_CAP_PER_SECTOR = 2`, `SCREEN_COOLDOWN_MIN = 45`, `SCREEN_LAST_ENTRY = 14:30`. One alert per name per session.
6. Score with the actual ledger's median initial risk **0.718%** and median cost **0.140R per trade**. A fixed stop at -1R is applied; hold-to-close is the optimistic upper bound.

### What it would have taken on 25 Nov

| Bar | Sector | Main pick | Side | Name | Rank / pos | Persist / counter | Directional close | Hold net R | MFE R | Stop? | Winner-trail proxy R |
|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| 09:35 | MEDIA | WAIT | short | SUNTV | 72.6 / #1 | 97 / 1 | +5.64pp | **+7.72** | 8.07 | no | +2.84 |
| 09:35 | MEDIA | WAIT | short | SAREGAMA | 67.9 / #2 | 97 / 1 | -0.84pp | **-1.14** | 1.94 | yes | -1.14 |
| 10:10 | FMCG | WAIT | long | HINDUNILVR | 81.9 / #1 | 90 / 1 | +2.86pp | **+3.84** | 4.07 | no | +1.43 |
| 10:10 | FMCG | WAIT | long | DABUR | 68.2 / #2 | 90 / 1 | +3.61pp | **+4.89** | 6.84 | no | +2.41 |
| 12:35 | MET | FMCG | short | TATASTEEL | 79.8 / #1 | 94 / 1 | +0.04pp | -0.08 | 1.81 | no | +0.64 |
| 12:35 | MET | FMCG | short | WELCORP | 79.3 / #2 | 94 / 1 | -0.64pp | **-1.14** | 0.00 | yes | -1.14 |

Full rows: `second_book_counterfactual.csv`.

### Bounds, not a backtest

| Measure | Six-trade second book |
|---|---:|
| Stop-aware close R | **+14.86R** |
| Hold-to-close net R, after 0.140R/trade cost | **+14.09R** (upper bound) |
| MFE R available | **+22.72R** |
| Winner-trail proxy: worst actual winner retained 35.2% of MFE net | **+5.04R** |
| All-ledger proxy: gross/MFE 16.5% then subtract cost | **+0.59R** |
| Stops | 2 of 6 |

The +14.09R figure assumes holding to 15:10. The real EMA9 trail would exit earlier. The winner-trail proxy uses the **worst** net/MFE conversion among this day's winners with MFE ≥ 1R (BAJFINANCE: 0.44 / 1.25 = 35.2%); the all-ledger proxy uses the whole ledger's gross/MFE (2.70 / 16.37 = 16.5%) and is very pessimistic. So the honest one-tape range for this extra book is **roughly +0.6R to +14.1R**, with about **+5R** as a more relevant trailing-exit center. Layered on the actual closed +0.14R, that is +0.7R to +14.2R before considering overlapping capital/risk limits.

Nearby parameter choices are not a knife-edge on this tape: varying the minimum rank from 55 to 80 gives **+9.78R to +16.46R** hold-to-close; requiring market `BULL`/`STRONG BULL` gives +14.99R; requiring main pick `WAIT` gives +14.92R. Those are sensitivity checks on **one correlated session**, not out-of-sample validation.

### Why this is the edge rather than “just remove the gates”

- Every final `earlier` refusal on this tape was a persistent top-5 leader, but blanket removal would also re-admit false early fires on other days.
- The `follower` trim did remove many poor names; mean directional last move for its 58 final labels was only +0.66pp. It should be **overridden only when a leader re-asserts**: top-2 position, rank ≥ 55, dominant sector persistence ≥ 90, counter ≤ 10.
- The winners came from **three sector books** (MEDIA short, FMCG long, MET short) on three separate trigger times. A rule that only fixes the current pick's name selection still misses MEDIA and IT.
- It uses the existing effective vocabulary (`SCREEN_*`, `LATE_LEADER_REBASE`, `TARGET_FALLBACK`, `PICK_CATCHUP`) but adds the missing piece: a **failover/second book that can trade a sector which is not the main pick**.

### Limits

- One day, one index regime (`BULL`, `TRANSITION`), 15 sectors, 72 five-minute closes.
- Board `mv` is close-to-open percentage, not intrabar high/low; fixed -1R stops are an approximation. No EMA9 series is present in the exports, so trailing exits cannot be replayed exactly.
- No slippage beyond the audit's 5 bps/side + ₹20/order model; no shared capital or correlation limit between the main book and second book; one stale cell and three missing post-close bars.
- Three sectors triggered on this tape. On a day when the first pick is already the right sector, this rule may add nothing, and on a choppy day it can add two correlated stop-outs.
- Validate on many sessions with cached raw boards and 1m bars before deployment. Accept only if the **lower-bound, post-cost** result and stop-out rate remain acceptable.

---

## 9. Secondary edge: monetize MFE before adding more signals

The cheapest improvement is not more entries; it is keeping more of the move already captured. Recovering **25% of the 8.72R EMA9 give-back** would add 2.18R gross and lift closed net from +0.14R to about **+2.35R**; recovering 50% would lift it to about +4.53R. Candidate mechanisms: partial exit at 1R/2R, a tighter trail after T1, or a chandelier/ATR trail instead of a pure EMA9 close trail. This needs true 1m or intrabar replay because the board only has 5m closes.

This does **not** solve the missed-winner problem; it solves the “right name, small realized R” problem. The two edges are complementary: second book finds the leaders, better trail keeps the move.

---

## 10. Code/config divergence — do not patch this checkout blindly

The audit's `rules.json` contains 70 effective knobs, including `SCREEN_BOT = 1`, `SCREEN_FAILOVER = 1`, `LATE_LEADER_REBASE = 1`, `PICK_CATCHUP = 1`, `TARGET_FALLBACK = 1`, `SCAN1M_EARLY_FROM = 09:30`, and the full `SCREEN_*` set. The current checkout at commit `d19e6f26f1634b0cb645a960ddc60f3f8ff62223` contains **none** of those strings in tracked files or local git history; GitHub code search for `SCREEN_BOT`, `SCAN1M_EARLY_FROM`, and related keys returned zero results. The workflows in this branch are M6/M15/M16 five-minute/quarter-hour runners, not a 1m workflow.

What *is* present here is the general architecture behind the failure:

- `nse_scanner/market.py::_complementary_pick()` selects the strongest persistent sector **on the same side as Nifty**.
- `nse_scanner/pipeline.py` scans only `md.pick_sector`.
- `nse_scanner/data.py` aggregates 1m OHLCV into NSE 5m bars, consistent with the board's “1m label / 5m data” mismatch.

Therefore the report was produced by a newer, private, or externally configured 1m runtime. Before changing code, identify/sync that runtime and add a regression replay from these cached exports. Otherwise a patch to this branch may not affect the bot that generated the audit.

---

## 11. Recommended next actions

1. **Preserve and replay**: `python results/scan1m_2026-11-25/analyze.py` regenerates every CSV/JSON here from the raw inputs. Use `board_name_bars.csv` + `board_sector_bars.csv` as the offline replay fixture.
2. **Instrument the runtime**: add counters for `second_book_candidates`, `reasserted_leaders`, `rebased_entries`, `refusal_reason`, `board_pos_at_entry`, and `cost_R_at_entry`. The current audit does not expose a `rebased_entries` count.
3. **Paper the second book** with the exact rule in section 8, but keep it separate from the main book's risk budget. Do not simply disable `follower`/`earlier`, and do not raise `SCREEN_MAX_DAY` before out-of-sample evidence.
4. **Add a board-rank gate to ordinary entries**: on this tape, #1–#2 entries made +3.51R while #3 and #4+ made -3.33R combined. Test `ENTRY_MIN_RANK` / `ENTRY_RANK_BAND` / `SECTOR_TOP_N` against more sessions before tightening.
5. **Replay exits on 1m bars**: quantify how much of the 8.72R EMA9 give-back a partial target or tighter trail would recover; include the 0.140R/trade cost in the acceptance test.
6. **Decision gate**: ship only if, across many sessions, the lower-bound post-cost second book is positive, the stop-out rate is tolerable, and it does not simply duplicate the main FMCG/FIN exposure.

---

## Files in this directory

| File | Contents |
|---|---|
| `README.md` | This analysis |
| `analyze.py` | Stdlib-only parser/replay that regenerates all derived files |
| `inputs/` | Raw user-provided JSON/HTML/PDF exports; ignored by Git, checksums in `metrics.json` |
| `board_name_bars.csv` | 13,680 point-in-time name-bar rows: `mv`, composite rank, board position, top-5 marker |
| `board_sector_bars.csv` | 1,080 sector-bar rows: bull/bear persistence, qualified, pick, day/intra, HTML pick cross-check |
| `name_summary.csv` | 190 name-sector summary rows with final-side directional last/peak/trough |
| `big_winners.csv` | 15 unique names with ≥3pp final-side move |
| `entry_quality.csv` | 15 actual entries with board rank/position, net R, MFE, give-back |
| `second_book_counterfactual.csv` | Six proposed second-book trades with close, stop, MFE, and trail-proxy R |
| `rules.json` | All 70 effective audit rules, exact default/effective values |
| `diagnostics.json` | Full diagnostics including `fresh_leaders`, `catchup_skip`, and `no_bar` |
| `metrics.json` | Input SHA-256 checksums, cross-checks, and every headline number used above |
