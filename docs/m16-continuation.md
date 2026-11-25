# M16 — independent small-pullback / BOS continuation alerts

M16 is an **alerts-only** model. It does not place orders or reuse M2/M6 trade ledgers, bot credentials, sector boards, persistent scores, leader ranks, sizing or exits. It imports M6's read-only history, normalization and feed adapters, so the universe and Yahoo chart transport are shared. M16 itself is Yahoo-only; it never invokes Dhan. It never writes M6 state.

## Rules (IST; five-minute candles)

1. Evaluate the existing master indicator causally, with **all exception switches enabled**, including BUY EX10/EX11. Track all final chart-label normal/EX BUY/SELL codes. Scanner-table previews 90/290 are excluded. Do not apply M2's trade-quality, OI, early-EX or open-position selection gates.
2. The initial signal must be **confirmed strictly before 11:00**. Thus a 10:50 candle confirmed at 10:55 qualifies; a 10:55 candle confirmed at 11:00 does not. Only completed candles create seeds.
3. Each original label creates an independently tracked seed. The BUY anchor is the lowest low of the last five available session candles through the seed candle; the SELL anchor is the highest high. This is an explicit causal approximation, not a claim to reproduce a TradingView drawing's proprietary swing algorithm.
4. Track the favorable rally extreme following the seed, but **do not qualify sideways action as a rally**. At the pre-pullback extreme, the last up-to-six completed candles (which may begin before the final signal, using only already-completed current-session candles) must contain at least three candles and two directional close-to-close advances, at least 0.3% net progress in the trade direction, directional efficiency >=0.65 (net progress / sum of absolute close changes), and both EMA9 and EMA10 sloping in the trade direction across that window. Store this evidence at the extreme, before the pullback—not using the later breakout. Also require at least 0.1% progress from signal close to the favorable extreme. These are explicit starting parameters, not a profitability-tested definition.

   **EMA continuity:** from the original signal candle through the BOS confirmation candle, BUY closes must remain at or above **both EMA9 and EMA10**. SELL closes must remain at or below **both**. A close through either EMA permanently invalidates that seed; touching/crossing with a wick or closing exactly on an EMA is allowed. Missing EMA data or a missing completed candle fails closed, because continuity cannot be verified. A provisional running breakout whose candle violates this rule receives `CLOSE_FAILED`, not confirmation.

   After a qualifying rally, on a **later, separate completed candle**, recognize either:
   - A wick intersecting the 23.6%–38.2% retracement band of the anchor-to-extreme leg; **or**
   - A wick touching EMA9 **or** EMA10. EMA calculations include prior-session warm-up, like the shared scanner frame.
5. Freeze the extreme as BOS when the pullback is recognized. Do not extend that BOS using future candles. A candle making a new favorable extreme cannot also establish the pullback: OHLC cannot reveal that intrabar order.
6. Small-pullback safeguard: retracement greater than 50% of the leg invalidates the setup. An anchor break also invalidates it. These are starting defaults, not profitability-optimized parameters.
7. BUY when observed running price crosses above frozen BOS; SELL mirrors below BOS. Use the current received candle close/last price, not the bar's historical wick. Stop after one outcome per seed. Multiple seeds converging on the identical symbol/side/BOS/pullback/break candle produce only one alert pair.
8. Both setup and breakout confirmation must occur **strictly before 12:30**. The latest eligible breakout candle is therefore 12:20–12:25; a 12:25–12:30 candle is excluded. Unfinished setups expire at 12:30.

## Two-stage alert contract

- **RUNNING BOS — unconfirmed:** first eligible observed price beyond BOS during a forming candle. Can fail before candle close. It is not a guaranteed fill at the BOS price.
- **CANDLE CLOSE — BOS CONFIRMED:** the same candle closes beyond BOS with structure intact.
- If that candle does not hold BOS/structure: **CANDLE CLOSE — BOS FAILED (not an entry)** instead of a false confirmed-entry alert.
- If no running crossing was observed but a fresh close qualifies, send a clearly labeled **CLOSE_ONLY** notification. Never fabricate an intrabar alert after the candle has finished.
- If the trigger candle is missing, record `CLOSE_UNAVAILABLE`; do not confirm using a different candle.
- Historical reconstruction records events but suppresses stale alerts. Default close/send freshness budget is 30 seconds. Failed sends retry within that budget with a short backoff. An outbox left `sending` after a crash is uncertain and is not blindly duplicated.

## Original indicator label on the BOS candle

If the **first/original final label itself appears on an already qualifying BOS candle**, M16 sends `DIRECT_BOS_CONFIRMED` immediately on its fresh completed-candle observation. It does **not** require a previous indicator label or wait for another pullback.

For this path, search the last up-to-24 completed session candles for the same directional rally, Fib/EMA pullback, EMA-close continuity and frozen-BOS conditions. Pattern anchors are a local price-pattern search, **not fabricated earlier indicator signals**. The setup retains the real original-label timestamp, plus its pattern-start timestamp and prior pattern evidence. The original label must still be confirmed strictly before 11:00. A naked breakout without the qualifying rally/pullback does not qualify.

A final original label cannot be known before its candle closes: this path is a clearly labelled close-confirmed alert, not a retroactive running alert. Late/replay observations are audit-only. If an older tracked signal already produces the same symbol/side/BOS close alert, annotate that alert with the coincident original label rather than send another duplicate. Confirmed direct entries appear in both EOD reports.

## Timing / feed limitations

Default watched-symbol poll cadence is one second (`M16_POLL_SECONDS`, minimum 1). Universe discovery is aligned to five-minute close +1 second before 11:00. Asynchronous fetches and separate active/discovery analysis workers prevent a whole-universe batch from holding up active setups. Late discovery bars are re-fetched within the initial freshness budget. Candle finality is evaluated against the request-start watermark, not the later processing clock: locally cached forming bars are never promoted to completed bars merely by waiting, and a request straddling the close needs a fresh post-close request before it can confirm that candle. This does not prove freshness of provider-side cached quotes.

**This does not guarantee one-second market-to-Telegram latency.** The shared Yahoo/Dhan five-minute bar APIs are not a real-time tick stream, can publish/cache forming bars late, and do not provide a trustworthy tick timestamp through this adapter. Request duration, rate limiting, computation, Telegram and GitHub Actions scheduling can all add delay. Alerts show observed times. A dedicated low-latency quote stream and continuously running host would be required for a stronger intrabar SLA. Multiple active requests and existing models share provider rate limits; monitor before increasing load.

## Separate Telegram bot activation

Create the new Telegram bot using BotFather, open/start it from the destination account/group, and configure **repository Actions secrets** via GitHub Settings → Secrets and variables → Actions:

- `M16_BOT_TOKEN`
- `M16_CHAT_ID`

Optional **second, independent M16 bot** (receives all running/close alerts, self-tests and the TXT/PDF reviews):

- `M16_BOT_TOKEN_2`
- `M16_CHAT_ID_2`

Set both mirror secrets, or leave both absent for single-bot mode. A half-configured mirror is ignored with a warning; an identical token/chat pair is deduplicated. The primary pair remains required. Delivery acknowledgements are recorded separately per destination using hashed identifiers (not credentials); a failed mirror retry does not resend to a primary that already accepted the alert, and vice versa. A crashed `sending` destination remains uncertain, while the other bot can still receive its copy. The self-test succeeds only when every configured destination confirms delivery.

Do **not** paste credentials in chat or commit them. These names never fall back to M6/default credentials. `DHAN_TOKEN` remains the optional shared market-data fallback token, not a bot token.

After the code is merged and the secrets are set, manually run workflow **16. LIVE M16 — pullback BOS continuation (alerts-only)** with mode `test-alert`. Verify the dedicated bot receives it, then run mode `session` from 09:15 IST. Entries stop at 12:30, but seeded-stock observation continues to market close. Reports are generated at approximately 15:31 IST. The workflow includes a schedule backup and the existing M15 workflow-completion trigger, but GitHub scheduling is best effort. No bot creation or successful live send is implied by offline tests.

The session refuses stale, outside-window and weekend starts. A continuous session is required for intrabar monitoring; `--once` is a diagnostic snapshot, not an adequate intrabar scheduler.

## Files and commands

- `m16_model.py`: causal setup state machine, cutoffs and event creation.
- `m16_runner.py`: feed/indicator integration, independent session and outbox.
- `state16.json`: daily seeds, cursors, phases, observations, outbox and diagnostics. Atomic writes; corrupt state fails closed. Date rollover starts a new ledger.
- `activity_M16_YYYY-MM-DD.txt`: attachment-only full-day review. Existing TXT delivery is retained.
- `audit_M16_YYYY-MM-DD.pdf`: detailed, paginated PDF audit with all saved data in an appendix, sent to both configured bots. TXT and PDF have independent per-bot delivery acknowledgements, so a failed PDF retry does not duplicate a successful TXT. Report-generation or delivery failure leaves EOD incomplete for retry; uncertain crash-sends remain available for manual review.
- `.github/workflows/16_live_m16.yml`: isolated workflow/concurrency; saves only M16 state/reports.

```sh
python -m unittest -v test_m16_runner test_m16_audit
python m16_runner.py --test-alert --dry-run
python m16_runner.py --test-alert
python m16_runner.py --session
python m16_runner.py --replay-date 2026-11-20 --symbols AXISBANK
```

Replay reads local history only, prints the audit, sends nothing and writes no state. It **cannot establish historical intrabar crossing times** from completed five-minute bars.

## Full-day PDF audit

The runner keeps a completed-candle journal with OHLCV and EMA9/10, initial indicator decisions (including invalid/excluded seeds), historical/live observation markers and a phase-change timeline. Every known seeded symbol, including invalid/failed/confirmed/expired seeds, remains under slower 30-second feed polling through the close. Only active pre-cutoff setups use the fast polling cadence. **Post-12:30 observations never generate new entries.**

The PDF contains:

1. Coverage, phase/event counts and explicit data-retention limits.
2. Rules and interpretation, including timing and feed limitations.
3. Every recorded indicator decision, with discovery time and reason.
4. Blocked/invalid/waiting/expired reason counts.
5. Each setup's signal, anchor, rally evidence, pullback, frozen BOS and final state.
6. Phase-change timeline with observation and source-candle timestamps.
7. All running and closing alert observations.
8. Later directional price extrema and last return relative to each alert's observed price. The event candle is excluded because its wicks could precede the intrabar crossing. These are **price observations, not executed returns or P&L**.
9. Per-bot alert and document delivery statuses, including stale/failed/uncertain sends. A report cannot contain acknowledgements received after its own creation.
10. Feed/engine diagnostics. Only the latest 2,000 diagnostic records are retained; the omitted-record count is explicitly reported rather than silently hidden.
11. Per-symbol candle coverage and absent expected bars. Non-seeded symbols are discovery-only, not falsely presented as watched all afternoon.
12. A complete saved-state appendix, without field truncation.

A missing/older journal is labelled unavailable. Future deployment cannot reconstruct earlier unrecorded intrabar observations. No sector/persistent analysis is invented because M16 has no sector gates. It is not a broker trade ledger, order-execution report or simulated SL/target strategy.

The GitHub session runs in shifts (324-minute maximum per invocation, persisted state followed by a handoff when needed) to stay below hosted-runner runtime limits. Backup schedules cover the afternoon; a 15:35 IST backup can retry EOD delivery. The runner accepts weekday starts until 15:40 IST for EOD retries. Successful collection still depends on the workflow actually running and the feed publishing data. The workflow preserves PDF, TXT and M16 state artifacts only.

## AXISBANK reference check

Using the repository's 20-Nov-2026 AXISBANK bars and the actual causal indicator:

- Original BUY-EX candle: **09:30–09:35**.
- Frozen BOS: **₹1,394.00**. The reference also passes the directional-rally and unbroken EMA-close filters.
- Recognized pullback candle: **10:20–10:25**.
- Confirmed BOS-break candle: **10:35–10:40**, close approximately **₹1,394.90**.

This matches the requested close-based chart example. The offline regression records it as `CLOSE_ONLY`, not an invented running-candle alert. Historical indicators/feeds can differ from TradingView; this single example is not a profitability validation.

### Live-feed incident hardening

M16 uses Yahoo only through `feeds.fetch_bars_yahoo`, the same Yahoo chart
fetcher used by M6. No Dhan API, token, or security mapping is required; even a
configured Dhan token is not used by M16. The shared M6 provider ladder is
unchanged. M16 separately checks session and completed-candle freshness: a
nonempty stale response is not success. The shared Yahoo fetcher handles its
query1/query2 retries and cookie/crumb refresh.

Active setups have a one-second minimum polling interval (one in-flight request
per symbol), not a one-second end-to-end alert guarantee. Discovery and terminal
research symbols retain slower polling to avoid hammering the whole universe.
Network time, Yahoo publication and throttling, worker queues, and Telegram
can all add latency. M6 also cannot guarantee one-second exchange-to-alert delay.

The live runner preserves per-symbol provider, latest candle, availability,
first failure and repetition counts. Repeated identical failures are aggregated.
Dedicated-bot health notices send at most one degradation and one healthy/recovery
notice per session. Persisted per-status delivery ledgers prevent repeats across
workflow restarts and health-status flapping. Monitoring and audit updates continue
without Telegram reminders. Failed notice deliveries retry on the next health check.
These are operational warnings, not trade alerts. Reports distinguish historical
and live-eligible events. Missing morning discovery is retried after 11:00 for
research/recovery; historical signals remain audit-only, never backdated alerts.

The workflow has a 09:00 IST prewarm dispatch, with the live loop waiting until
09:15. GitHub scheduled starts are best effort, not an exchange-grade scheduler.
The existing shift handoff remains in place. Check a morning run actually starts,
and that both dedicated bot destinations work. A healthy poll is not proof of tick freshness: OHLC polling
cannot guarantee every transient intrabar crossing. Provider quotas and Telegram
latency still apply. Zero missed/delayed alerts requires a monitored always-on
streaming service and verified provider entitlements; this patch does not claim
that guarantee. Do not widen the 30-second entry budget to hide outages.

Offline regression command:
`python -m unittest -v test_m16_feed test_m16_runner test_m16_audit`

#### Follow-up timing review

Discovery retry readiness is judged using the snapshot's request-start watermark,
not the later loop clock. Thus a cached forming candle cannot suppress the
required post-close refetch. Telegram outbox freshness advances across the
entire batch (including time spent delivering earlier events), not just within
each event. Health notices compare candle coverage against the expected close
instead of treating normal five-minute universe polling as a 90-second outage.
When history availability is known, missing history or a lagging analysis cursor
also prevents a false healthy notice, even if Yahoo fetches succeed.

Regression verification: 62 M16 feed/model/audit tests, full M6 runner suite,
Python compilation, workflow YAML/configuration and embedded shell syntax checks.
These are offline checks, not a live Yahoo publication or Telegram latency SLA.

Health-notice upgrade also recognizes an already delivered legacy notice, so an
ongoing outage does not generate another warning just because the code restarted.
Failed destinations may retry; destinations that acknowledged delivery are skipped.
Trade/BOS notifications and their freshness rules are unchanged.

### Regular-session holiday guard

`m16_calendar.py` contains the NSE **equities trading** holiday table for 2026,
including 24 November (Prakash Gurpurb Sri Guru Nanak Dev) and the 15 January
Maharashtra election closure. Source:
https://www.nseindia.com/resources/exchange-communication-holidays
The separate, longer **clearing** holiday table must not be used to suppress
trading on otherwise-open weekdays.

The workflow checks the calendar using only Python's standard library before
installing dependencies or starting a session. Known holidays/weekends skip
scanner startup and self-handoff. The runner independently guards startup,
live-session entry and health notifications, so a manual invocation cannot
mistake a known holiday for a Yahoo outage. No holiday trade alerts, degraded
notices, history loads or state overwrites are produced. An explicit dedicated
bot self-test remains available on closed days.

Dates are evaluated in Asia/Kolkata. Missing Yahoo candles NEVER determine
holiday status. Unknown calendar years remain eligible on weekdays, with an
explicit console warning; update the maintained holiday table annually and for
new exchange circulars. Unexpected closures will still appear as monitoring
problems, not guessed holidays. Special/Muhurat sessions outside the regular
09:15-15:30 schedule are not supported by this regular-session strategy.

Startup health checks also wait until 09:20:30 IST: the first five-minute candle
closes at 09:20, with the normal 30-second publication allowance. Lack of a
completed candle at 09:16 is not a legitimate initial analysis failure.

### 25 November outage recovery

See `docs/m16-2026-11-25-review.md` for measured incident timings and limits.
M16 still uses the shared Yahoo fetcher, now with optional sanitized diagnostics
and M16-only shorter request timeouts. Other callers retain their existing
transport defaults. On empty/stale one-day results M16 tries five days, filters
to today's aligned five-minute candles, and applies the same freshness budget.
Unaligned trailing quote timestamps do not certify candle coverage.

Persistent empty responses can invalidate M16's cached Yahoo session at most
once per two minutes; HTTP 429 instead triggers a shared 30-second cooldown.
Per-symbol failure retries back off up to 30 seconds. Missing discovery continues
retrying after a candle's first 30 seconds, but delayed entries are still never
backdated. Receipt recency (five seconds) and request duration (at most 30 seconds)
are separate from the request-start completed-candle watermark.

Outage start/recovery episodes, sampled ongoing failures, current health reasons
and minute progress counters are retained. Once-per-session bot notice dedupe
remains intact. Missing discovery cannot become healthy just because 11:00 passed
without any tracked seeds. End-of-job artifacts preserve diagnostic state even
if pushing the Git state fails. Artifact upload is not a live intraday snapshot
service; the running job's console supplies intraday progress.

### Concurrent-main merge reconciliation

The merged workflow retains supervised 110-minute shifts, a 12-start daily cap,
a read-only `--feed-probe`, isolated probe/dry-run concurrency groups, and
a five-minute sustained widespread-outage watchdog requesting a new supervised
runner. A reported HTTP Retry-After extends the M16 rate-limit cooldown. The shared
transport retains its typed `YahooFetchError` API as well as the M16 diagnostic
callback and timeout override.

Optional Dhan fallback and per-episode repeated bot notices from the concurrent
main changes are intentionally not enabled: the user requested Yahoo-only data
and no repeated operational warnings. Once-per-session dedupe remains in force.
Validation after reconciliation: 87 M16 tests and the full M6 runner regression.

### Pre-signal rally context (no gainers/losers filter)

A final signal starts tracking. Rally evidence may use up to six contiguous
completed current-session candles before/through that signal. Normal pullback
and BOS must follow tracking; the authorized direct-original-BOS exception stays.
Future context and preview labels remain excluded.

The NSE top gainers/losers gate was removed at the user's request. Neither All
Securities nor F&O rankings restrict normal M16 alerts; there is no NSE movers
fetch dependency. Yahoo prices, freshness checks, timing windows and separate
M16 Telegram destinations are unchanged. Older EOD comparison reports in results/
are historical experiments only, not current filters.

Replay saved labels/EMA values (no Telegram, no P&L reconstruction):
`python m16_backtest.py --audit activity_M16_2026-11-25.txt --output results/m16_backtest_normal_2026-11-25.json`

### Approved persistence-only sector gate

Production M16 requires its home sector to rank in the directional persistent
**top 3** with score **strictly >70**. BUY uses persist_bull; SELL persist_bear.
This uses M6's board engine and sector map, not its full entry_ok gate, stock
composite top-5, leadership tolerance, flow qualification, or follow-through rules.
NSE gainers/losers filtering remains removed. M6 itself is unchanged.

A dedicated process refreshes the current completed-candle board using Yahoo
index/basket prices and cached prior history. It never calls M6 trading/state or
Telegram functions. Complete current inputs and all 30 sectors are required.
Missing/stale boards fail closed and enter the existing session-deduplicated
monitoring-health notification path. Failures retry every five seconds; active
stock polling remains best-effort one second and does not wait on sector work.
Snapshots and per-event gate evidence persist in the M16 full saved audit.

At a running breakout only an already-received board for the most recent closed
candle may be used. A new close invalidates the previous board immediately: no
future candle board is backdated to an earlier running breakout. Entries and
confirmations require eligibility; failure/unavailable-close updates remain risk
notices, not gated new entries. Send-time guards recheck freshness and eligibility
between destinations. Pending pre-gate alerts cannot bypass this requirement.

The supplied M6 audit comparison is a candle-close sensitivity only: 30 technical
candidates -> 10 persistence-qualified (versus 7 with full M6 sector rules).
This does not imply 10 real-time running alerts or improved realized profits.
