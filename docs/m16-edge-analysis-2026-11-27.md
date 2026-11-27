# M16 — where the edge actually is (and why 27 Nov produced false trades)

**Scope:** the four sessions whose logs exist — 2026-11-23, 11-25, 11-26, 11-27
(24 Nov is not in the uploaded set). 115 M16 events, 76 distinct
`(symbol, side, candle)` setups, 209 symbols × 11,652 five-minute audit bars.

**Method.** Section 12 (`COMPLETE SAVED AUDIT`) of each `activity_M16_<date>.txt`
contains a Python-literal state dump, including `audit_bars` (raw 5-min OHLCV,
`ema9`, `ema10`) for every symbol plus `sector_snapshots` (39 boards on the 27th).
Every number below is recomputed from those raw bars, not from the report's own
summary tables. Per event the path starts **after the event candle** and runs to
15:25. Barrier races are resolved **conservatively**: if one candle touches both
the target and the stop, the stop is assumed first. Alerts only — no fills, fees
or P&L are asserted anywhere.

---

## 1. The one-paragraph answer

M16 has a real, measurable edge — it finds stocks that move: mean best excursion
**+1.17%** vs mean worst **−0.87%**, mean drift to the close **+0.29%**, and 55%
touch +0.5% before −0.5% even under the pessimistic same-candle rule. But the edge
is **entirely consumed by *where* you enter**, and the sector gate — the component
that decides which 15 of 58 events are sent — currently has **negative** selection
value. On 27 Nov the gate passed the ten worst-performing breakouts (longs in the
strongest morning sectors) and rejected ten of the best (shorts in the weakest).
The reason is structural, not bad luck: the gate scores a sector from its own
recent returns, and the alerting stock is inside that average, so the gate is
grading the trade **by the five minutes that already happened after the BOS**.
It therefore admits the extended entries and rejects the paused ones. The
"false trades" are the predictable output of that design.

---

## 2. Today's book: what you actually received

58 events → 51 distinct setups → 25 gate blocks → 28 outbox items →
**15 messages covering 10 distinct setups** (5 duplicates), 13 `sector_rejected`,
**0 `expired_unsent`** (the board-wait fix from PR #59 held).

| # | time | symbol | side | kind | alert px | ext % | vol ×med | gate | best | worst | close |
|---|------|--------|------|------|---------:|------:|---------:|------|-----:|------:|------:|
| 1 | 09:45 | MPHASIS | SELL | DIRECT_BOS | 2452.50 | 0.22 | 0.83 | pass (IT 73, r1) | +0.77 | −1.08 | −0.12 |
| 2 | 10:00 | INDIANB | BUY | RUNNING | 951.25 | 0.03 | 0.84 | pass (PSUBK 89.5, r2) | +0.06 | **−2.65** | **−2.49** |
| 3 | 10:00 | INDIANB | BUY | CLOSE | 951.25 | 0.03 | 0.84 | pass (PSUBK 94.4, r2) | +0.06 | −2.65 | −2.49 |
| 4 | 10:05 | COCHINSHIP | BUY | RUNNING | 1130.00 | 0.01 | 2.05 | pass (DEF 99.1, r1) | +1.24 | −0.88 | −0.39 |
| 5 | 10:05 | HAL | BUY | RUNNING | 5033.10 | 0.05 | 3.48 | pass (DEF 99.1, r1) | **+2.27** | +0.20 | +0.83 |
| 6 | 10:05 | HAL | BUY | CLOSE | 5053.40 | 0.45 | 3.48 | pass (DEF 99.4, r1) | +1.86 | −0.20 | +0.43 |
| 7 | 10:05 | COCHINSHIP | BUY | CLOSE | 1133.00 | 0.27 | 2.05 | pass (DEF 99.4, r1) | +0.97 | −1.15 | −0.65 |
| 8 | 10:10 | POWERINDIA | BUY | DIRECT_BOS | 31020.00 | 0.13 | 2.85 | pass (CAPGOODS 77, r3) | +0.79 | −0.76 | −0.06 |
| 9 | 10:15 | RVNL | BUY | CLOSE | 190.83 | 0.04 | 0.92 | pass (RAILWAYS 83, r3) | +0.93 | −0.55 | −0.07 |
| 10 | 10:30 | INFY | SELL | DIRECT_BOS | 1154.00 | 0.14 | 1.54 | pass (IT 90, r1) | +0.22 | −0.99 | −0.02 |
| 11 | 10:35 | BEL | BUY | RUNNING | 413.65 | 0.05 | 2.92 | pass (DEF 99, r1) | +0.12 | −1.34 | −0.81 |
| 12 | 10:35 | BEL | BUY | CLOSE | 413.90 | 0.11 | 2.92 | pass (DEF 99.7, r1) | +0.06 | −1.40 | −0.87 |
| 13 | 11:15 | AMBUJACEM | SELL | CLOSE | 387.20 | 0.18 | 0.98 | pass (CEMENT 74.8, r2) | +0.67 | −0.23 | +0.46 |
| 14 | 11:25 | ULTRACEMCO | SELL | RUNNING | 12639.00 | 0.02 | 1.75 | pass (CEMENT 75.8, r2) | +1.29 | −0.15 | +0.89 |
| 15 | 11:25 | ULTRACEMCO | SELL | CLOSE | 12641.00 | 0.01 | 1.75 | pass (CEMENT 77.8, r2) | +1.31 | −0.13 | +0.90 |

**7 of the 10 distinct setups closed below the alert price.** The distribution is
not "many small misses": one alert (INDIANB, sent twice) is −2.49% and accounts
for the whole day's damage.

### The same day, filtered out by the gate

These were `score ≤ 70`, `rank > 3`, or post-detection board rejections. Nothing
was sent to you:

| symbol | side | time | gate | best | worst | close |
|--------|------|------|------|-----:|------:|------:|
| BAJAJ-AUTO | SELL | 10:40 | block (AUTO bear 22.7, r14) | +2.27 | −0.35 | **+2.04** |
| ANGELONE | BUY | 10:30 | block (53.2, r10) | +2.16 | −0.35 | +0.57 |
| ABCAPITAL | SELL | 09:50 | block (21.3, r12) | +2.07 | −0.15 | +0.39 |
| GODFRYPHLP | BUY | 09:40 | block (FMCG 5.4, r30) | +1.94 | −0.29 | +1.12 |
| ETERNAL | SELL | 11:00 | block (31.9, r13) | +1.37 | −0.27 | +1.00 |
| GAIL | SELL | 10:55 | block (40.6, r6) | +1.03 | −0.13 | +1.02 |
| LICI | SELL | 10:50 | block (unknown) | +1.04 | +0.05 | +0.80 |
| AXISBANK | SELL | 10:50 | block (22.6, r13) | +0.91 | −0.12 | +0.73 |
| BAJAJFINSV | SELL | 10:50 | block (43.9, r4) | +0.96 | −0.25 | +0.70 |
| SBIN | SELL | 10:50 | block (20.5, r19) | +0.71 | −0.25 | +0.54 |

Setup level (deduped by symbol/side/candle): **sent −0.16% mean drift vs
not-sent −0.00%**. The gate did not select; it inverted. Three of the largest
misses were pure `unknown` (stale board) → re-checked against a *later* board →
rejected.

---

## 3. Why the false trades happened — five mechanisms

### 3.1 The gate is a rear-view mirror (the big one)

`sector_metrics()` builds `persist_bull/bear` from the sector index's own returns:
`day_ret`, `rret`, RS vs NIFTY, position in the day's range, and the share of the
last N bars above EMA20 — then EMA-smooths it. Nothing in it is forward-looking,
and the alerting stock sits **inside that average**. So a stock that pops on the
BOS candle lifts its own sector score, and a stock that stalls lowers it.

Three independent measurements:

* **Lead–lag.** Panel of 30 sectors × 48 boards (26th+27th), score vs same-bar
  mean member return, shifted ±4 bars:

  | bars | −4 | −3 | −2 | −1 | 0 | +1 | +2 | +3 | +4 |
  |------|----|----|----|----|---|----|----|----|----|
  | corr | .164 | .172 | .163 | .160 | −.003 | −.007 | .030 | .036 | .019 |

  Negative lag = *the return came first*. The score leads nothing.

* **Forward value is small but real.** Rank IC of the sector score vs the sector's
  mean member return over the next 30 min: **+0.19** (sd 0.47, n=39). Top-3 sectors'
  members beat the rest by **+0.08pp** (26th) and **+0.06pp** (27th) per 30 min.
  That is one tenth of the alert's own ±1.2% dispersion — and it is the ceiling on
  what a hard gate can buy you.

* **INDIANB, minute by minute.** PSUBK board vs the stock:

  | time | 09:20 | 09:35 | 09:50 | 10:05 | 10:20 | 10:35 | 10:50 | 11:05 | 11:20 |
  |------|------:|------:|------:|------:|------:|------:|------:|------:|------:|
  | PSUBK score | 54.8 | 63.7 | 89.3 | **94.4** | 90.0 | 70.5 | 68.3 | 53.1 | 41.6 |
  | INDIANB % | 0.00 | +0.90 | +0.92 | +1.46 | +1.48 | +0.82 | +0.23 | +0.20 | −0.59 |

  The alert fired at 10:00, when the score printed 94.4 (one bar *after* the
  breakout) and the price was 951.25 — 0.03% above BOS, 99% of the session range,
  on **0.84× median volume**, against a 09:45 bar that had done 56k shares vs the
  breakout's 16k. Every ingredient of an exhaustion high, and the gate called it
  rank 2 of 30.

* **The mirror on the short side.** MPHASIS SELL passed on IT `persist_bear` after
  IT had already fallen 1.3% into 10:05; the alert went out 0.5% off the low and
  bounced +1.08% against it. To be allowed to short, the gate requires the sector
  to have already fallen — i.e. it short-sells extended down moves.

The same distinction exists inside the BUY family, and it is the cleanest single
statement of the edge: **BUY-EX needs a wick through the day high with price
merely "not far" from VWAP (`vwapnotfar`) — 39% hit rate, zero expectancy.
BUY-EX17 needs the candle to *close* above the day high with price *above*
VWAP — 90% hit rate, +0.78% drift.** A wick through the high is a liquidity
sweep; a close through the high is an accepted breakout. M16 currently treats
both as "BOS confirmed".

**Consequence:** the gate is a momentum-confirmation filter with a 5-minute
look-ahead through the sector aggregate. It selects the entries that have already
run. Quantitatively: `corr(run-up at alert, sector score) = +0.48`, and the
selected subset is exactly the extended one (INDIANB 0.03% ext is the exception;
HAL-CLOSE 0.45%, COCHINSHIP-CLOSE 0.27%, MPHASIS 0.22%).

### 3.2 Half the messages are duplicates, and the duplicate is the worse trade

49% of setups (37 of 76; 25 of 51 today) emit **two** events on the same candle —
`RUNNING_BREAK` at detection and `CLOSE_CONFIRMED` at the close. When both pass
the gate you get two messages for one trade, and the second is priced worse for
the direction:

| setup | first alert | second alert | penalty | outcome of the two |
|-------|------------:|-------------:|--------:|--------------------|
| HAL 10:05 | 5033.10 | 5053.40 | +0.40% (BUY) | +0.83 / +0.43 |
| COCHINSHIP 10:05 | 1130.00 | 1133.00 | +0.27% (BUY) | −0.39 / **−0.65** |
| BEL 10:35 | 413.65 | 413.90 | +0.06% (BUY) | −0.81 / −0.87 |
| INDIANB 10:00 | 951.25 | 951.25 | 0 | −2.49 / −2.49 |
| ULTRACEMCO 11:25 | 12639 | 12641 | +0.02% (SELL) | +0.89 / +0.90 |

COCHINSHIP is the clean case: the running alert was a **winner** (+1.24% best),
the close alert on the same candle was a **loser** (−1.15% worst). You were told
both.

### 3.3 The alert price is the worst price of the next two hours

* 56% of alerts print their **worst** level before their best; median time to the
  worst print **80 min**, to the best **110 min**.
* Only **28%** reach +0.5% within 30 min; 67% ever reach it; median 80 min.
* A limit **0.3% better** than the alert fills before a +0.5% move **60% of the
  time** (before +0.25%, 44%). At 0.5% better: 45%.

So "enter at the printed price" is systematically paying the top of the local
range. The alert text prints `@ price` with no limit level and no risk frame, which
invites a market entry at the worst print of the sequence.

### 3.4 Extension (chasing) is the single most consistent killer

Distance of the alert price beyond BOS, in the trade direction, pooled over all
115 events:

| ext % | n | ±0.5% win | mean best | mean worst | drift to close | E(+1.5/−1.0) |
|-------|---|-----------|----------:|-----------:|---------------:|-------------:|
| ≤ 0.05 | 45 | 53% | +1.09 | −0.89 | +0.08 | +0.12 |
| **0.05–0.2** | 46 | **61%** | +1.34 | **−0.61** | **+0.78** | **+0.51** |
| 0.2–0.5 | 21 | 52% | +1.07 | −1.29 | −0.17 | −0.32 |
| > 0.5 | 3 | 0% | +0.28 | −1.43 | −0.68 | −0.54 |

Same direction on 4 of 4 days. The complement (`ext > 0.2` OR `vol > 2.5×`) has
**negative** expectancy at 1.5/1.0 on 3 of 4 days.

### 3.5 Volume climax and the opening hour

| vol × median | n | win ±0.5% | drift | | window | n | win ±0.5% | mean worst | E(+1.5/−1.0) |
|---|---|---|---|---|---|---|---|---|---|
| < 1.0 | 37 | 57% | +0.27 | | 09:45–10:15 | 59 | 41% | −1.04 | +0.11 |
| **1.0–2.0** | 45 | 53% | **+0.65** | | 10:15–11:15 | 49 | **65%** | −0.74 | +0.17 |
| 2.0–3.5 | 20 | 55% | −0.07 | | 11:15–12:15 | 7 | 100% | −0.27 | +0.83 |
| > 3.5 | 13 | 54% | **−0.32** | | | | | | |

The opening window is a **volatility regime, not a direction**: biggest mean best
excursion (+1.31) *and* biggest mean worst (−1.04). With a 0.5% stop it is a
losing coin flip (−0.09); with a 1.0% stop it is positive (+0.11). 59 of 115
events fire there, so this is where most of the felt "false trades" live.

---

## 4. Where the edge is

Pooled expectancy of the whole alert stream (gross, no costs): **+0.11%** per
alert at +1.0/−0.5, **+0.18%** at +1.5/−1.0. Thin. Conditioned:

| cohort | n | win ±0.5% | drift | E(+1.5/−1.0) | days positive |
|--------|---|-----------|------:|-------------:|---------------|
| all events | 115 | 55% | +0.29 | +0.18 | 4/4 |
| `ext ≤ 0.2 & vol ≤ 2.5` | 77 | 58% | +0.48 | +0.37 | 4/4 |
| `+ kind ≠ DIRECT_BOS_CONFIRMED` | 62 | 58% | +0.31 | +0.25 | 3/4 |
| **`R1 & signal ∈ {BUY-EX17, SELL-EX1, NORMAL SELL}`** | **30** | **80%** | **+0.97** | **+0.74** | **4/4** |
| complement of R1 | 38 | 47% | −0.09 | −0.22 | 1/4 |

Signal codes are the master indicator's own chart labels and are the strongest
single discriminator:

| code | meaning (master_scanner.py) | n | win | drift | E(+1.5/−1.0) |
|------|----------------------------|---|-----|------:|-------------:|
| BUY-EX17 | new day high **closed** above the day high + price above VWAP | 10 | 90% | +0.78 | +0.44 |
| SELL-EX1 | exception sell entry; no relative volume spike, entry candle ≤ first-5m size | 16 | 69% | +0.61 | +0.60 |
| NORMAL SELL | base sell entry | 12 | 67% | +0.48 | +0.36 |
| BUY-EX6 | — | 2 | 100% | +0.63 | — |
| BUY-EX11 | retest fail + first 5m not day high | 10 | 40% | +0.41 | +0.25 |
| **BUY-EX** | **base exception buy: wick through the day high, VWAP only "not far" (1/3 of all events)** | **38** | **39%** | **+0.04** | **−0.04** |
| SELL-EX3 | — | 7 | 43% | +0.19 | — |

And the gate's own state variables, measured against outcomes (26th+27th, n=40):

| sector score Δ over last 15 min | n | win | drift |
|---------------------------------|---|-----|------:|
| ≤ −5 | 7 | 71% | +0.03 |
| **−5 … −1** | 7 | **0%** | **−1.01** |
| −1 … +1 | 5 | 40% | −0.70 |
| **+1 … +5** | 12 | **83%** | **+0.43** |
| > +5 | 9 | 44% | +0.05 |

Level alone is *inverted*: score ≥ 85 → drift −0.81; score ≤ 50 → +0.18. Breadth
too: when 4–7 sectors sit above 70, drift is −0.52 (37% win) vs +0.24 (67% win)
when ≤ 3 are. **A high, flat-or-falling score in a crowded board is the danger
state** — and it is precisely the state the current gate selects.

---

## 5. What I would change, in order of expected value

1. **Dedupe per `(symbol, side, bar)`** — send one message, from the earlier
   `RUNNING_BREAK` (or the `CLOSE_CONFIRMED` if no running alert went out), and
   record the suppressed twin in state. Removes ~half the messages and the
   worse-priced duplicate without losing a signal. Lowest risk, immediate.
2. **Re-base the gate on the score's slope, not its level/rank.** Require
   `Δscore(15m) ≥ 0` (ideally `> +1`) and drop the relative top-3 requirement, or
   keep `rank ≤ 3` as a *soft* context line only. Add a breadth guard: if more
   than ~7 sectors are above 70, the board is crowded → no hard pass. This inverts
   the current bias (it currently admits the extended and rejects the paused).
   Must be A/B'd on live data — 15 boards of history is not enough to prove a
   gate.
3. **Label the entry, don't just print it.** Emit `BOS <level>` and a retest limit
   (`BOS + 0.05…0.2%` region) as the preferred entry, and mark anything with
   `ext > 0.2%` as `EXTENDED — retest or skip`. 60% of alerts come back to 0.3%
   better before running; the alert price is currently the worst print of the next
   two hours more often than not.
4. **Time-aware framing.** Before 10:15 the same signal has a 41% hit rate at
   ±0.5% but the largest excursions; either widen the implied risk frame in the
   message ("early session — noise, expect ±1%") or hold tight-stop alerts until
   10:15. Do not widen the 12:30 cutoff; the 11:15–12:15 cohort is the best of the
   day (n=7, +0.83).
5. **Tier by signal code.** BUY-EX17 / SELL-EX1 / NORMAL SELL as tier 1; BUY-EX
   (a third of the stream, 39% hit rate, ~0 expectancy) as tier 2 — send it, but
   marked. This is a labelling change, not a filter, so it costs no coverage.
6. **Report semantics fix.** `live` currently means "promoted after a sector wait",
   so §8 prints "live-eligible 11" while 15 messages were delivered (the four
   `DIRECT_BOS_CONFIRMED` sends never set it). The audit should count *delivered*
   events; today's report under-counts its own sends by 4.
7. **Rejections against a later board.** All 13 `sector_rejected` items had
   `sector_gate: unknown` at detection and were rejected by the first board that
   published afterwards — a board that necessarily contains the post-alert candle.
   If the gate survives (2), a stale-at-detection event should be judged by a rule
   that excludes the stock's own contribution, or simply be sent as
   `UNCONFIRMED SECTOR` rather than rejected on a board it helped build.

---

## 6. What the numbers do **not** support

* **Not** "the alerts are bad". Mean best +1.17% vs mean worst −0.87% with a
  positive drift on all four sessions is a genuine, repeatable asymmetry.
* **Not** "the board-wait latency is eating alerts". 0 `expired_unsent` today vs
  42 events / 9 expired on the 26th — PR #59 fixed that. The four `DELAYED`
  re-entries (30–37 s) all printed at the discovery price, not worse.
* **Not** "high sector score is good". It is the opposite in this sample; the
  current threshold of 70 with a relative top-3 is doing the work, and it is
  doing it backwards.
* **Caveat on sample size.** 115 events / 4 sessions. The `R1 + tier-1 signal`
  cohort (n=30) and the Δscore buckets (n=7, n=12) are hypotheses with consistent
  signs, not proofs. Anything acted on should first run as a labelled shadow
  cohort for a week.

---

## 7. Reproduce

Everything derives from `activity_M16_<date>.txt` §12 only:

* path metrics — from the first bar after `event.bar` to 15:25 (best/worst/close,
  time to each, barrier ladder with the stop assumed first inside a shared candle);
* `ext` — `sign(side) × (price − bos) / bos × 100`;
* `vol_ratio` — volume of the breakout candle ÷ median of the prior 20;
* gate state — `sector_gate.score/known_rank/sector` at the event, and
  `sector_snapshots[*].sectors[<sector>].persist_bull|persist_bear` for Δ and IC;
* forward IC — mean member 5-min return over the next 30 min per sector board,
  Spearman against the score, 30 sectors × 39 boards (27th) + 9 (26th).

---

## 8. Why the defence names were missed (added after the 27-Nov review)

### 8.1 GRSE, PARAS (and DATAPATTNS, ASTRAMICRO, BEML): outside the universe

M16's universe is `fno_universe.csv` — **210 F&O names** — and its sector map is
`fno_sector_map.csv`, whose `DEF` basket is exactly BDL, BEL, COCHINSHIP, HAL,
MAZDOCK, SOLARINDS. GRSE, PARAS, DATAPATTNS, ASTRAMICRO and BEML have **no F&O
contract**, so they were never in `shared.L.SYMS`: no fetch, no engine run, no
seed, no alert. No gate or threshold fix could ever have surfaced them.

Fix shipped: `m16_extra_universe.csv` (+ `M16_EXTRA_UNIVERSE=1`) adds those five
as `DEF` to the **scanning** list only. They never join a sector basket, so no
sector score changes.

That file now carries **196 major liquid non-F&O names across 32 sector labels**
— the rest of the defence complex (MTARTECH, ZENTEC, AZAD, DCXINDIA, IDEAFORGE),
capital goods (THERMAX, HONAUT, SCHAEFFLER, TIMKEN, SKFINDIA, TRITURBINE),
travel (IRCTC), chemicals (NAVINFLUOR, DEEPAKNTR, AARTIIND, ATUL), cement
(JKCEMENT, RAMCOCEM, JKLAKSHMI), pharma (ABBOTINDIA, AJANTPHARM, NATCOPHARM),
metals (JSL, HINDCOPPER, JINDALSAW), realty (BRIGADE, SOBHA), textiles
(KPRMILL, TRIDENT), media (SUNTV, PVRINOX, ZEEL), sugar, shipping and so on.
Same rule as the five: scan-only, never in a basket. `TEXTILE`/`MEDIA`/`SUGAR`/
`SHIPPING` have no basket, so those alerts print `sector unverified` instead of
a score — honest context rather than a fabricated one.

### 8.2 BDL: in the universe, tracked, then lost — twice

BDL seeded `BUY-EX` at 09:30 and ran 1041 → 1092.75 by 10:40. Then:

| bar | close | EMA9 | verdict |
|-----|------:|-----:|---------|
| 11:15 | 1082.3 | 1084.4 | **0.19% below EMA9 → seed invalidated, permanently** |
| 12:25 | 1089.8 | — | still under the 1092.75 BOS |
| 12:30 | 1092.0 | — | first wick above BOS (cutoff candle) |
| 12:45 | 1092.8 | — | first **close** above BOS — 15 min past the cutoff |

So BDL needed **two** independent fixes and would still not have alerted today:
the EMA tolerance (now 0.30%, shipped) would have kept the seed alive, but the
breakout itself happened at 12:30–12:45, after the strict 12:30 confirmation
cutoff. That cutoff is a deliberate rule, not a bug — changing it is a policy
decision (see below).

### 8.3 MAZDOCK and SOLARINDS: same rule, no actual loss

Both died on the same shallow EMA close (MAZDOCK 11:10, 0.05% under; SOLARINDS
11:15, 0.06% under). Neither then made a new high for the rest of the session
(MAZDOCK peaked 1915.7 at 10:30, ended 1890.4; SOLARINDS peaked 19,520 at 10:40,
ended 19,489), so no alert was lost — only the visibility was.

### 8.4 Scale of the rule

Across the four sessions: **358 seeds invalidated by EMA continuity** versus
**115 continuation alerts** — the rule discards three ideas for every one it
delivers, and 339 of the 358 (95%) were closes within 0.30% of the EMA. That is
the justification for `M16_EMA_TOLERANCE_PCT=0.30`, measured, not guessed.

### 8.5 The one lever that is still closed

A post-cutoff observation alert (`POST-CUTOFF — observation only, not an entry`)
for BOS breaks between 12:30 and 15:00 would have caught BDL, and it cannot
affect the entry rules because it would be labelled and excluded from the entry
contract. It is **not implemented**: the 12:30 cutoff is a stated rule of the
model and changing it silently would be wrong. Say the word and it becomes a
one-flag addition (`M16_POST_CUTOFF_OBSERVATIONS=1`).
