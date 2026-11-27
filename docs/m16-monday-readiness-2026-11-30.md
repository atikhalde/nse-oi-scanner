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

## Second verification pass — 27-Nov, 22:00 IST (re-run on merged main `6a68f527`)

Everything below was executed again from a fresh checkout of the merged `main`
and a rebuilt virtualenv.

**One bug found and fixed on this pass.** `_heal_html()` — the one-shot retry
after Telegram answers `400 can't parse entities` — re-escaped entities that were
already correct, so a retry of an `M&M` alert showed `M&amp;M` literally and its
href grew an extra level of escaping. Healing is now **idempotent**: entities
emitted by this module are parked while the text is escaped and restored
afterwards, so a correctly escaped message comes back byte-identical, while a raw
`&`/`<`/`>` is still escaped. Covered by two new checks in `test_telegram_bot.py`
and a label assertion in `test_m16_runner.py`.

**Alert behaviour matrix — 16/16 PASS** (`charts.dhan.co` key, Adjust app link,
same-stock fallback, no foreign hosts, exactly two anchors, id/no-id extras,
`M&M` quoting, heal keeps both anchors and a clean label, heal idempotent, plain
text has no anchors but keeps the app URL, body line unchanged, `M16_APP_LINK=0`,
`M16_APP_DEEPLINK` override, labelled self-test sample, delayed alerts).

**Workflow invariants — 21/21 PASS**: bootstrap `15 3 * * 1-5` / 90 min /
`M16_EXTRA_UNIVERSE=1` / id refresh + id map committed + F&O verify + rebase
retry; M16 `25 3 * * 1-5` and `5 10 * * 1-5` / 350 min / all eight modes with a
run branch / probes excluded from state commits / advisory sector mode,
duplicate suppression, EMA tolerance 0.30 %, late alerts, 3 discovery slices,
0.20 s Yahoo spacing / CI regression module list intact.

**Data**: 406 universe (210 F&O + 196 extra, no duplicates); 400 files carry
2026-11-27; no missing or stale *active* name; ICICIPRULI dormant; the four
Yahoo-less extras remain the only gaps and are dormant; 30 sectors, 63 active
basket members, no empty basket.

**State**: rollover produces a clean 2026-11-30 ledger (`eod_done: false`,
0 events, 0 outbox, 0/12 automatic starts); calendar says 30-Nov `open`
(regular Monday) and 28-Nov `closed` (weekend).

**Tests (all re-run)**: `test_m16_runner` 147 OK · CI invocation **256 tests OK**
(transport 13, sector 29, rally 4, calendar 19, feed 33, audit 11, runner 147) ·
`test_m6_runner` ALL PASSED · `test_telegram_bot`, `test_m16_feed/sector/rally/
audit/calendar/transport`, `test_fast_feed`, `test_m15_runner`,
`test_m15_data_feed`, `test_master_scanner`, `test_trader` OK.

`--readiness-check`: `ready: true`, target 2026-11-30, `missing_history: []`,
`stale_prior_day: []`, `workflow.issues: []`.

---

## Third pass — what the real 2026-11-27 bootstrap log exposed (fixed)

The 16:38Z `2. Bootstrap` run **passed** (`ok=402 total=406 missing_fno=[]`,
`missing_extra=['ATHERENERGY','GUJGASLTD','GSPL','JBCHEPHARM']`, commit
`ad81ce84` 403 files, push attempt 1/6, F&O freshness 209/210 dormant 1). Two
real defects were visible in the same log and are now fixed:

1. **Dhan ids under-covered.** `DHAN IDS ok=400 agreed=209/209
   unresolved=['ICICIPRULI','ATHERENERGY','HEG','GUJGASLTD','GSPL','JBCHEPHARM']`.
   `parse_master()` kept only `SEM_SEGMENT` `E/EQ` NSE rows, so a name listed in
   the trade-to-trade (`BE`) or illiquid (`BZ`) series was dropped even though the
   master carries its id; those names fall back to `NSE:<SYMBOL>` chart keys. Now
   `E/EQ/BE/BZ` are accepted (ordinary equity series preferred), and the refresh
   prints `unresolved_detail` from `parse_master(text, watch=…)`, which says
   whether an unresolved name lives under another exchange/series
   (`NSE/BE/EQUITY`, `BSE/BE/EQUITY`) or is `NOT LISTED` — the difference between
   a filter to relax and a ticker to retire. The master downloads once
   (`fetch_text`) and is parsed twice. Which case HEG/GSPL/GUJGASLTD/JBCHEPHARM/
   ATHERENERGY/ICICIPRULI are in is decided by the next refresh run (no egress to
   Dhan from CI to check locally).
2. **Empty `DHAN_TOKEN`.** Every step of that run printed `DHAN_TOKEN:` (blank):
   the repo secret is missing, so the Dhan candle fallback and the Dhan feed were
   silently disabled. `2_bootstrap.yml` gained a first `check the Dhan token
   secret` step that raises `::warning title=DHAN_TOKEN is empty::` and prints the
   token length when present. **Action for the operator:** add the secret
   (Settings → Secrets and variables → Actions). Alerts themselves are unaffected
   — the M16 feed is Yahoo-only and id resolution needs no credentials.

Also fixed while in there: `dhan_ids.load()` no longer leaks the file handle
(it emitted `ResourceWarning` under `-W error` hosts), and the universe-limit
refresh test no longer prints the whole 406-name universe.

**Tests (all re-run after this pass):** `test_m16_runner` **151 OK**
(14 `DhanIdTests`, new: `BE` series kept and equity series wins, `watch` reports
where an unresolved name lives, refresh logs `unresolved_detail`, `fetch_text`
skips an empty body); CI invocation **260 OK** (transport 13, sector 29, rally 4,
calendar 19, feed 33, audit 11, runner 151); `test_telegram_bot` PASS ·
`test_fast_feed` PASS · `test_m15_runner` OK · `test_m15_data_feed` OK ·
`test_master_scanner` ALL CHECKS PASSED · `test_m6_runner` ALL PASSED.

`--readiness-check`: `ready: true`, target 2026-11-30, prior 2026-11-27, 406
universe / 400 active, dormant `[ICICIPRULI]`, `missing_history: []`,
`stale_prior_day: []`, `extra_missing_history: [ATHERENERGY, GSPL, GUJGASLTD,
JBCHEPHARM]` (Yahoo gaps, dormant), 30 sectors / 63 basket members,
`workflow.issues: []`.
