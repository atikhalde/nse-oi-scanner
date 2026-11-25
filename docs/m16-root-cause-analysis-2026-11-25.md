# M16 root-cause analysis — 25 Nov 2026

> Historical analysis draft preserved from concurrent main changes. Specific
> rate-limiting/authentication claims below are hypotheses, not established by
> the saved audit. For the measured findings and current Yahoo-only recovery
> design, see `m16-2026-11-25-review.md` and `m16-continuation.md`.

## Bottom line

**M16 is not failing because of Telegram, its bot secrets, or an absence of original signals.** It is failing because the long-lived M16 runner loses its Yahoo market-data path, then keeps hammering the blocked/rate-limited endpoint without a circuit breaker. M16 deliberately refuses to alert on bars observed more than 30 seconds after their candle close, so every later recovery is audit-only and produces no outbox item.

The precise short version:

> **No fresh completed/running candles after 09:30–09:35 IST → no armed BOS setups → no events → no outbox → no Telegram alert.**

The Telegram path itself has acknowledged messages on both configured M16 destinations. The strategy state machine also works in offline replay. The primary defect is in **live feed acquisition/recovery and workflow isolation**, not the BOS rules.

No production code was changed by this analysis.

---

## 1. What the current `state16.json` proves

Current committed state is for `2026-11-25`:

| Field | Observed value |
|---|---:|
| Original tracked seeds | **29** |
| Events | **0** |
| Outbox items | **0** |
| Telegram delivery attempts for trade alerts | **0** |
| Completed-candle cursors | all **209 symbols stuck at 09:30 IST** |
| Recorded bars per symbol | exactly **4**: 09:15, 09:20, 09:25, 09:30 |
| Setup phases at end | all **29 expired** |
| Expiry reason | `confirmation cutoff reached (12:30 IST)` |
| `feed_health` | **210/210 symbols `ok: false`** |
| Repeated feed failures | **324,776** |
| Approx. Yahoo HTTP attempts | **~649,552** (two query hosts per failed fetch) |
| First active-symbol failures | **09:36:53 IST** |
| Whole-universe failures begin | **09:40:01 IST** |
| Last recorded failures | **14:25:08 IST** |
| EOD done | **false** |

The 29 seeds were created from the 09:30 candle at about 09:35 IST. They were valid `rally` setups, including BUY-EX, BUY-EX11, BUY-EX17 and SELL-EX variants. They never received the next completed candle, so they could not develop a rally extreme, recognize a pullback, freeze a BOS, or emit an alert. At 12:30 they were mechanically expired.

`audit_bars` likewise contains only 09:15–09:30 for every symbol. This is a data-coverage failure, not a strategy rejecting 29 setups.

### Telegram is demonstrably working

`state16.json` contains two hashed M16 destinations, both marked `sent` for the 09:20 health notice. The 23 Nov EOD state also has TXT and PDF marked `sent` for both destinations. Therefore:

- `M16_BOT_TOKEN` / `M16_CHAT_ID` are configured;
- the optional second M16 bot is configured;
- `telegram_bot.send_message_to` / `send_document_to` worked;
- `deliver_targets()` and `flush()` were not even reached for trade alerts because there was no event to enqueue.

A `--test-alert` is still worth running after any fix, but missing credentials are not the current cause.

---

## 2. The exact code path that suppresses an alert

### 2.1 Feed failure is reduced to `empty response`

`m16_feed.fetch()` calls `feeds.fetch_bars_yahoo(symbol, "1d")` and then `current_bars(...)`.

- `feeds.fetch_bars_yahoo()` catches **every** request/JSON exception and returns `None`; it only prints the exception to stdout.
- `m16_feed.current_bars(None, now)` raises `ValueError("empty response")`.
- `m16_feed.fetch()` wraps that as `no usable M16 feed: yahoo: empty response`.

Consequently the persisted state cannot distinguish:

- HTTP 429 rate limiting;
- HTTP 401/403 cookie/crumb rejection;
- HTTP 404 from a Yahoo host;
- timeout / connection reset;
- JSON parse failure;
- a valid chart response with no `timestamp`.

This loss of diagnostics is itself a significant defect. Given the request volume and the simultaneous all-symbol failure, an HTTP 429 / Yahoo crumb-session rejection is the most likely upstream condition, but the exact status was swallowed and cannot be recovered from the committed state.

### 2.2 The polling design creates the outage

`m16_runner.live_session()` schedules:

- the full 210-symbol universe at every five-minute discovery bucket before 11:00;
- repeated discovery refetches inside each 30-second freshness window;
- **every active setup at a one-second interval** as soon as seeds exist;
- 16 discovery fetch workers plus 8 active fetch workers;
- after 11:00, a “recovery” loop that adds every symbol whose cursor is earlier than 10:50.

The last item is especially damaging:

```python
if not model.before(now, "11:00:00") and (not cursor or model.stamp(cursor).strftime("%H:%M") < "10:50"):
    symbols.add(sym)
```

Because all cursors stayed at 09:30, **all 210 symbols remained in the recovery set for the rest of the day**. Non-active symbols were then refetched every 30 seconds. Active symbols were refetched once per second until 12:30. There is no cap, no exponential backoff, no `Retry-After` handling, and no global pause after widespread failure.

`feeds.fetch_bars_yahoo()` immediately tries `query1` and then `query2` for each failed fetch, so the state’s 324,776 failed fetches correspond to roughly 650,000 HTTP requests. This is a self-sustaining request storm: once Yahoo throttles the runner, M16 continues hard enough that it never gets an opportunity to recover.

M15’s shorter workflows were still able to record successful Yahoo calls on the same day. That is consistent with either:

1. a GitHub Actions runner egress IP / Yahoo session being throttled for the long-lived M16 job while fresh short-lived runners get another IP; or
2. the long-lived M16 process retaining a poisoned cookie/crumb session.

Both are made persistent by the 5h24m single session and the lack of a restart/backoff mechanism.

### 2.3 M16 has no fallback provider

The other models use a Yahoo → Dhan → local-history ladder. M16 deliberately uses **Yahoo only**:

- `m16_feed.fetch()` calls `feeds.fetch_bars_yahoo` directly;
- `security_id` is ignored;
- Dhan is never tried;
- local `data/history/*.csv` is used only as prior-session indicator warm-up, not as current-session bars.

`feeds.py` itself describes Yahoo as “~15min delayed”. A delayed free chart endpoint polled once per second is not a suitable primary source for a 30-second intrabar alert contract. The documentation acknowledges this limitation, but the implementation still makes live alerts entirely dependent on it.

### 2.4 Stale observations are intentionally audit-only

`m16_runner.MAX_LAG = 30`.

In `m16_model.closed_bar()`:

```python
fresh = 0 <= (now - end).total_seconds() <= max_lag
```

`emit(..., live=fresh)` only adds an outbox item when `live=True`. `event_fresh()` applies another strict 30-second check during `flush()`. A `RUNNING_BREAK` must additionally be sent before its forming candle closes.

Therefore, when a feed comes back minutes or hours later, M16 may reconstruct historical setups/events for the PDF, but it correctly refuses to backdate Telegram alerts. On 25 Nov it never even reconstructed events because Yahoo remained unavailable to that runner.

**Do not fix this by simply widening `MAX_LAG`.** That would send stale “live” breakouts. Fix the feed.

---

## 3. Workflow and monitoring amplifiers

### 3.1 One long session blocks fresh runners

Workflow `16_live_m16.yml` uses:

```yaml
concurrency:
  group: live-m16-continuation
  cancel-in-progress: false
```

and starts:

```sh
python -u m16_runner.py --session --max-runtime-min 324
```

On 25 Nov run `45540416619` started at **03:30:13 UTC / 09:00 IST** and ended at **08:55:19 UTC / 14:25 IST**. During that entire interval, later M16 dispatches were cancelled or queued by the concurrency group. A fresh GitHub runner—and therefore a fresh IP/session—could not take over when Yahoo throttling began at 09:36.

The handoff run `45580536934` started at **08:55:16 UTC / 14:25 IST** and was still in progress as of approximately 14:40 IST. It can finish the EOD report after 15:31, but it cannot recover alerts whose 30-second freshness windows expired in the morning.

### 3.2 The health-notice dedupe hides the real outage

At **09:20:30 IST**, M16 sent a degraded notice for only one symbol (`ICICIPRULI`) and persisted `health_notices.degraded.complete = true`.

When all 210 symbols began failing around 09:40, `notify_health()` saw that a degraded notice had already been completed and returned without sending another message. `health_notice.checked_at` later advanced to 14:24 and `affected` grew to the 29 seeded symbols, but no new notification was delivered.

Additional monitoring defects:

- after 11:00, `watched` includes only symbols with existing setups, so a total failure of unseeded discovery is not fully represented;
- after a degraded notice is complete, a later widening of the outage is silent;
- the healthy/recovery notice is also status-deduped; on 24 Nov an earlier version even sent a “healthy” message on an NSE holiday because no symbols were considered watched.

### 3.3 Scheduled runs are not the primary trigger

The observed 25 Nov M16 runs are `workflow_dispatch`, not the workflow’s own `schedule` or `workflow_run` events. Internal GitHub schedules are best effort; external/manual dispatch is doing most of the work. This is an operational fragility, but the main session did start at 09:00 IST, so it is not the cause of the zero-alert day.

---

## 4. Historical pattern: this is recurring, not a one-day anomaly

| Date | Evidence | Live alerts |
|---|---|---:|
| 20 Nov 2026 | Offline AXISBANK reference replay produces one setup and one `CLOSE_ONLY` event at 10:35; `live=False` because replay is deliberately send-free. Model logic works. | 0 by design |
| 23 Nov 2026 | Long run 09:15–14:40 IST. **644,730 diagnostics omitted** by retention cap. Around 12:16–12:17 a late feed recovery/backfill generated 84 setups and 7 qualifying events. Every event has `live=False`; outbox is empty. EOD TXT/PDF were sent to both bots. | **0** |
| 24 Nov 2026 | NSE holiday. A long session started before the holiday guard was committed at 14:07 IST; it made **112,560** failed Yahoo fetches, created no cursors/setups, and later sent an incorrect healthy notice. | 0, expected on holiday |
| 25 Nov 2026 | Fresh 09:15–09:30 bars and 29 seeds, then total Yahoo failure from ~09:36/09:40 through 14:25. **324,776** failed fetches; no events/outbox. | **0** |

The 23 Nov report is particularly important: it proves the model can find valid BOS continuations when bars eventually arrive, while also proving that late backfill is deliberately excluded from live alerts.

The holiday guard in `m16_calendar.py` is now present and correctly treats 24 Nov as closed; that specific issue should not recur while the 2026 calendar remains maintained.

---

## 5. What is *not* the issue

- **Not Telegram credentials:** two hashed destinations have successful delivery acknowledgements.
- **Not `alert_text()` or `flush()`:** no event ever reached `st["outbox"]`.
- **Not the BOS/rally/pullback state machine:** 75 M16 offline tests pass, and the 20 Nov reference replay produces the documented `CLOSE_ONLY` event.
- **Not a lack of original labels on 25 Nov:** 29 final labels were tracked at 09:30.
- **Not the 12:30 cutoff:** the cutoff merely expired setups that had already been starved of candles.
- **Not a market holiday on 25 Nov:** `m16_calendar.session_status()` correctly treats Tuesday 25 Nov as a regular session.
- **Not an EOD-report failure:** the report is generated after 15:31 and is separate from entry alerts.

---

## 6. Recommended fix, in priority order

### P0 — Make the feed observable

1. Change `feeds.fetch_bars_yahoo()` to raise or return a typed result containing `status_code`, host, symbol, exception class and `Retry-After`. Do not collapse everything to `None` + a stdout print.
2. Persist that category in `feed_health`, while continuing to aggregate repeated identical failures.
3. Add a one-shot diagnostic command/workflow that performs a handful of Yahoo requests and prints status/crumb health without starting a full session.

### P0 — Add a real rate-limit circuit breaker

1. Global token bucket/shared limiter across all M16 fetch threads.
2. Honour `Retry-After`; on 429/401/403, pause **all** symbols with exponential backoff and re-handshake the cookie/crumb once.
3. Do not immediately retry `query2` after a 429 from `query1`.
4. Cap concurrent Yahoo requests to a small number (for example 2–4), not 24.
5. Stop retrying 210 stale cursors after a bounded number of attempts or a fixed post-11 recovery window; never continue the recovery storm until 15:30.

### P0 — Use a feed that matches the alert contract

For one-second “RUNNING BOS” alerts, use an authorized low-latency quote/websocket feed or a continuously running host with provider entitlements. At minimum, restore a valid Dhan token and add M16 fallback support. Yahoo’s delayed chart API should be a last-resort/backfill source, not the sole intrabar trigger.

A GitHub Actions 5h24m polling job cannot provide a tick-time SLA.

### P1 — Allow clean restart

1. Add a watchdog: if all/most `feed_health` entries have failed continuously for N minutes, save state and exit.
2. Let workflow concurrency start a fresh runner; consider `cancel-in-progress: true` for a demonstrably unhealthy old session.
3. Keep the durable per-symbol cursors and outbox so a restart does not duplicate or backdate alerts.
4. Consider splitting the day into shorter supervised sessions rather than one 324-minute process holding the concurrency group.

### P1 — Fix health notices

Send on genuine state transitions or severity escalation, not once per status per day. After 11:00, include discovery-cursor health. Always send an explicit recovery notice after a total outage. Include HTTP status categories in the message/report.

### P1 — Add realistic failure regressions

Current 75 offline tests pass, but they do not simulate sustained Yahoo 429s. Add tests for:

- all symbols returning 429 for 10 minutes;
- backoff/circuit-breaker state;
- no post-11 recovery storm;
- fresh restart preserving cursors/outbox;
- late bars producing audit-only events, never Telegram sends;
- holiday guard;
- warning escalation and recovery.

### P2 — Operational cleanup

- Ensure exactly one reliable 09:00 IST prewarm/session trigger; remove duplicate external/internal dispatch races.
- Keep the NSE holiday table current annually.
- Run `python m16_runner.py --test-alert` after deployment.
- Do not loosen model filters or increase `MAX_LAG` merely to make stale data produce messages.

---

## 7. Diagnosis confidence

**High confidence:**

- zero alerts were caused by zero fresh bars / zero events / zero outbox items;
- Yahoo-only M16 feed failed continuously after early morning;
- the runner generated a very large retry storm with no recovery;
- Telegram and model state-machine paths are functional;
- late observations are intentionally suppressed by the 30-second freshness rule;
- long-session concurrency prevents fresh-runner recovery;
- health-notice dedupe hid the expanded outage.

**Inferred, not yet directly logged:**

- the exact upstream HTTP status was most likely 429 or Yahoo cookie/crumb rejection. The code currently discards that detail, so it must be instrumented before claiming a single provider-side status code.

The first code change should therefore be **error propagation plus a circuit breaker**, followed by replacing Yahoo polling with a feed capable of meeting M16’s live-alert timing contract.
