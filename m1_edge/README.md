# M1E — separate paper second-book model for the 1m scan

This is the **opt-in, paper-only, alerting** implementation of the edge found in
`results/scan1m_2026-11-25/README.md`. It is **not a silent shadow book**.

When enabled, M1E sends its own clearly labelled **distinct `[M1E · PAPER SECOND
BOOK]` cards to the same 1m Telegram bot** — bot 3, via
`telegram.send_bot3` — while the original 1m scan, its alerts, entries, and
`positions` stay intact.

## Why this is a sidecar patch, not a change to the current scanner

The exact runtime that produced the 2026-11-25 “1m” audit is **not in this
checkout**. The current repo is `atikhalde/nse-oi-scanner`; the 1m projected
scanner lives in the separate public repo `atikhalde/Persist-check-`:

- `nse_scanner/scan1m.py` — minute loop, `scan1m_state.json`, bot 3
- `nse_scanner/board.py` — `sweep()` builds the point-in-time board payload
- `nse_scanner/telegram.py` — `send_bot3()` sends to the same 1m bot
- `.github/workflows/live-scan-1m.yml` — `NSE 1m projected scan`, bot 3

Here, `live_runner.py` uses “M1” as a model label, and workflow 15 is a 5m M15
workflow. Patching those would not test the real 1m report runtime. Therefore:

1. `m1_edge/model.py`, `runner.py`, and `test_m1_edge.py` are a standalone,
   stdlib-only model in this branch.
2. `m1_edge/integration/persist-check-1m-second-book.patch` is a ready-to-apply
   patch for the **external 1m runtime**. It adds the same model as
   `nse_scanner/edge_second_book.py`, calls it from `scan1m.sweep_board()` and
   `scan1m._end_of_session()`, injects `telegram.send_bot3`, and adds its own
   workflow env/cache/artifact.
3. Default is `M1E_ENABLED=0`. When enabled, `M1E_MODE=paper` and
   `M1E_ALERT=1`: the book is paper-only but **visible on the same bot**.

No code in the current checkout’s original scanner path has been changed.

## The second-book rule

On each `board.sweep()` payload, the model independently evaluates:

- sector gate open (`gate`/`qualified`);
- dominant persistence `>= 90`, opposite persistence `<= 10`;
- top two board-position names (`pos <= 2`) with composite `rank >= 55`;
- current dynamic sector side, levels rebased to the current board candle;
- max `2` names/sector, `6`/day, `45` min sector cooldown, last entry `14:30`;
- paper exit at `-1R`, optional `+TARGET_R`, otherwise EOD;
- audit-calibrated `risk_pct=0.7178237971021002`, `cost_r=0.14013902190524707`.

`board.sweep`’s raw `mv` is close-to-open; `from_board_payload` converts it to
`side * mv`, so positive `mv` means the paper trade is winning.

## Distinct alerts on the same bot

The hook injects `telegram.send_bot3` into the model. M1E never calls
`telegram.send`, `send_bot4`, or any order API, and never falls back to bots
1/2/4. A new-entry bar produces one card like:

```text
<b>[M1E · PAPER SECOND BOOK]</b>
2026-11-25 · 09:35 · <b>PAPER</b>
<b>2 new second-book entries</b>
BUY FMCG HINDUNILVR #1 rank 80.0 · persist 95/2 · main pick WAIT
BUY FMCG DABUR #2 rank 70.0 · persist 95/2 · main pick WAIT
Paper only — no live order. Original 1m book unchanged.
```

Stop/target/EOD events get their own M1E cards, and EOD produces a separate
`[M1E · PAPER SECOND BOOK — EOD]` summary. Alerts are deduped in M1E’s own
state; if Telegram returns failure or the runner hands over, pending cards are
retried on the next board bar. `DRY_RUN=1` prints bot-3 cards instead of
sending, using the same `send_bot3` path.

### Separate state and logs

| Purpose | Path |
|---|---|
| Paper ledger + alert dedupe | `state/edge_second_book_state.json` |
| Per-bar journal + alert deliveries | `reports/m1_edge_<session>.jsonl` |
| External workflow cache key | `nse-1m-edge-state-*` |
| External journal artifact | `m1-edge-<session>-<run>-<attempt>` |
| Delivery channel | **same bot 3** via `telegram.send_bot3` |

Original paths deliberately untouched: `state/scan1m_state.json`,
`state/scan_state.json`, `state/screen_state.json`, bot 3’s original messages,
and the main `sent`/`positions` book.

## Apply to the real 1m repo

From a clone of `atikhalde/Persist-check-`, on its own testing branch, copy the
patch into that clone (or use its absolute path):

```bash
cp /path/to/nse-oi-scanner/m1_edge/integration/persist-check-1m-second-book.patch .
git apply --check persist-check-1m-second-book.patch
git apply persist-check-1m-second-book.patch
python -u test_edge_second_book.py
```

Then, in that repo’s GitHub **Settings → Secrets and variables → Actions →
Variables**, set `M1E_ENABLED=1`. Leave `M1E_MODE=paper` and `M1E_ALERT=1`.
Dispatch `NSE 1m projected scan`. The original bot-3 command and alerts stay
unchanged; the second book writes its own state and sends its own `[M1E]`
cards to the same chat.

The patch was checked with `git apply --check` against the current external
`Persist-check-` main HEAD, and its external tests pass in a reconstructed copy
of that source.

## Local replay / regression

```bash
python -u test_m1_edge.py
python -m m1_edge.runner \
  --audit-json results/scan1m_2026-11-25/inputs/audit-2026-11-25-1m.json \
  --session 2026-11-25 --replay --print-alerts \
  --entries-out results/m1_edge_replay/entries.csv
```

`--print-alerts` uses a local stdout sender, so the distinct cards are visible
without Telegram. The raw audit/HTML inputs under `inputs/` are gitignored, so
CI runs the stdlib unit tests plus a synthetic bot-3 card smoke test.

Locally, the replay reconstructs `board.sweep`-shaped payloads from the durable
CSVs and reproduces the audit’s six second-book entries:

`SUNTV`, `SAREGAMA`, `HINDUNILVR`, `DABUR`, `TATASTEEL`, `WELCORP` —
`6` entries, `2` stops, net `+14.085325R`, MFE sum `+22.721453R`. This is the
audit’s hold-to-close upper bound, not a forward result.

## Configuration

All are opt-in and default to the audit values. `M1_EDGE_*` is accepted as an
alias for `M1E_*`.

| Variable | Default | Meaning |
|---|---:|---|
| `M1E_ENABLED` | `0` | master switch |
| `M1E_MODE` | `paper` | hard-gated; no live/order mode. Legacy `shadow` normalises to `paper` |
| `M1E_ALERT` | `1` | send distinct M1E cards to same bot 3 |
| `M1E_ALERT_PREFIX` | `M1E · PAPER SECOND BOOK` | distinct card label |
| `M1E_ALERT_EXITS` | `1` | alert stop/target closes |
| `M1E_ALERT_EOD` | `1` | send distinct EOD summary |
| `M1E_MIN_PERSIST` | `90` | dominant persistence floor |
| `M1E_MAX_COUNTER` | `10` | opposite persistence ceiling |
| `M1E_MIN_RANK` | `55` | composite board-rank floor |
| `M1E_TOP_N` | `2` | board-position ceiling |
| `M1E_MAX_PER_SECTOR` | `2` | sector cap |
| `M1E_MAX_DAY` | `6` | day cap |
| `M1E_COOLDOWN_MIN` | `45` | per-sector cooldown |
| `M1E_LAST_ENTRY` | `14:30` | no entries after this IST bar |
| `M1E_RISK_PCT` | `0.7178…` | audit median initial stop |
| `M1E_COST_R` | `0.1401…` | audit median round-trip cost |
| `M1E_TARGET_R` | `0` | `0` = no fixed target, EOD otherwise |
| `M1E_REQUIRE_GATE` | `1` | require sector gate |
| `M1E_MIN_STRENGTH` / `M1E_MIN_STRUCTURE` | `0` | optional board-leg filters |
| `M1E_STATE_PATH` / `M1E_JOURNAL_DIR` | see above | override isolated paths |

Setting `M1E_ALERT=0` is the only way to silence the paper book; it is not the
default. There is no live-order mode.

## Forward-test acceptance before touching the original

Keep the original book intact until **all** of these are true on new sessions:

- many independent days, not the single correlated 2026-11-25 tape;
- lower-bound post-cost result after realistic exits is positive;
- stop-out rate and max drawdown fit the separate risk budget;
- board-rank/persistence gates remain stable out of sample;
- true 1m bars/intrabar highs-lows are used to validate the `-1R` proxy;
- journals show no shared-capital/correlation surprise with the original book;
- M1E cards are readable on bot 3 without confusing original 1m alerts.

Only then consider tightening ordinary-entry gates or merging the edge. Until
then, M1E is a separate, visible paper book.
