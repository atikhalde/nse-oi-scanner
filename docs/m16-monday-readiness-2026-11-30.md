# M16 — Monday 30-Nov-2026 readiness (audited 27-Nov, 21:30 IST)

Audit of everything shipped on 27-Nov (PR #65 → #73) plus the two open issues it
left behind. Every check below was run in this checkout; the numbers are the
actual output, not a plan.

## Verdict

**Ready.** `python m16_runner.py --readiness-check` returns `"ready": true` for
target session **2026-11-30** with expected prior session **2026-11-27**, and the
full suite is green (256 tests in the CI 7-module invocation, all 14 test modules
run individually).

## Alert format (unchanged from the last confirmed version)

```
M16 · CANDLE CLOSE — BOS CONFIRMED
<b><a href="https://charts.dhan.co/?symbol=NSEE5900:AXISBANK">AXISBANK</a></b> · BUY @ 1259.70
BOS 1247.10 · pullback … · tier B
Entry +1.01% beyond BOS · …
Breakout candle 09:45 · observed 09:45:20 IST
Feed-polled observation, not a guaranteed tick-time fill. Alerts only; no live order.
📱 <a href="https://dhan.go.link/?adj_t=…&adj_deeplink=dhan%3A%2F%2FLogin&adj_fallback=…">open in Dhan app</a>
```

- Symbol → that stock's chart on Dhan's own `charts.dhan.co` (desktop and phone
  browser). Key is `NSEE<security-id>:<SYMBOL>` from `fno_universe.csv`, the same
  form Dhan's own landing page uses (TCS → `NSEE11536:TCS`).
- `📱` line → Dhan's own Adjust entry link, so Android opens the **app**; the
  fallback is the same stock's chart. Dhan has no in-app chart route (12 probed
  routes, see `m16-continuation.md`), so the app lands on its home.
- Both links survive the 400 parse-reject retry (`_heal_html` preserves
  `<a href>`/`</a>` and folds a double-escaped `&amp;amp;` back to `&amp;`).

## What was fixed in this audit

1. **Extra/mapped names had no Dhan id.** `fno_universe.csv` only ids the 210 F&O
   names, so the 196 extras got neither the Dhan candle fallback nor the pinned
   chart key. New `dhan_ids.py` + `dhan_security_ids.csv`: a best-effort,
   self-validating refresh of Dhan's instrument master
   (`live_runner.py --refresh-dhan-ids`, wired into workflow 2 before the
   download). A parsed map is accepted only when it agrees with the trusted F&O
   ids (≥ 90 % of ≥ 100 checked), so a changed layout writes nothing.
   `live_runner.SID` keeps `fno_universe.csv` authoritative on every overlap.
2. **Universe gaps made actionable.** `ATHERENERGY, GSPL, GUJGASLTD, JBCHEPHARM`
   have no history file at all and `HEG` is stale at 2026-09-21 — Yahoo does not
   serve them. With ids they now reach the Dhan fallback on the next 08:45 run.
   Until then they stay dormant: no seeds, no polling, no alerts.
3. **Workflow-mode documentation drift.** The `16_live_m16.yml` mode description
   did not mention `test-alert`/`app-link-probe`; corrected.
4. **Test artifact hygiene.** `test_m6_runner.py` writes `state6.json`,
   `activity_M6_*.txt`, `learn/raw_M6_*.csv` and `paper_test_M6_*.xlsx` into the
   checkout. They were reverted after each run in this audit; nothing reached a
   commit.

## Verified state (21:30 IST, 27-Nov)

| Check | Result |
| --- | --- |
| Calendar | 2026-11-30 is a regular session (Monday), prior session 2026-11-27 |
| Universe | 406 symbols = 210 F&O + 196 extra/mapped; 400 active |
| History | 400/402 files carry 2026-11-27; `missing_history: []`, `stale_prior_day: []` |
| Dormant | ICICIPRULI (F&O, series stopped 2026-10-22) — excluded from polling |
| Extra gaps | ATHERENERGY, GSPL, GUJGASLTD, JBCHEPHARM (no file), HEG (stale 2026-09-21) — dormant, not alerts |
| Sector board | 30 sectors (17 index, 13 basket), 63 active basket members, no empty basket, no missing history |
| State | on disk `date 2026-11-27`, `eod_done: true`; rollover target `2026-11-30`, `eod_done: false`, automatic starts 0/12 |
| Workflow | cron 08:55 session / 15:35 EOD retry, 08:45 bootstrap, windows inside 08:55–15:40; `issues: []` |
| Telegram | not configured in this sandbox (secrets live in Actions) — the last two runs in Actions completed `success` |

## Tests run in this audit

| Suite | Result |
| --- | --- |
| `test_m16_runner.py` | 147 tests OK |
| CI invocation (`test_m16_transport sector rally calendar feed runner audit`) | **256 tests OK** |
| `test_m6_runner.py` | ALL PASSED |
| `test_telegram_bot.py`, `test_m16_feed/sector/rally/audit/calendar/transport`, `test_fast_feed`, `test_m15_runner`, `test_m15_data_feed`, `test_master_scanner`, `test_trader` | OK |

## Monday checklist (operator, 08:40 IST)

1. Workflow **2. Bootstrap** (08:45) ends with `BOOTSTRAP RESULT …` and a
   `DHAN IDS ok=N …` line. F&O gaps must be empty; extra gaps are a warning.
2. Workflow **16. LIVE M16** (08:55) starts a session; the first alert carries
   both links — the symbol opens the Dhan chart, `📱` opens the Dhan app.
3. If anything looks off before the open, run mode `readiness-check`, then
   `test-alert` (sends a labelled sample, never a signal).
4. Entry cutoff stays 12:30; a post-cutoff observation alert is offered
   explicitly, never silently.

## Known limits (unchanged, deliberate)

- The Dhan app cannot open a chart from a link — no chart route, and no Dhan host
  serves an App Links file. `M16_APP_DEEPLINK` accepts a route the moment Dhan
  ships one.
- Chart links for names without an id fall back to `NSE:<SYMBOL>`; after the
  Monday id refresh every name should carry the pinned `NSEE<id>:` form.
- History comes from Yahoo (60 d) with a Dhan fallback; a name Yahoo stops
  serving stays dormant until it appears in the Dhan master with an id.
