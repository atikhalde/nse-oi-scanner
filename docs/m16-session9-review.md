# Fresh review: uploaded session 9

## Observations, not assumptions

The uploaded session began 26 November 2026 at 11:27:44 IST and was manually cancelled at 11:30:33. Heartbeats continued at 11:28:04, 11:29:05 and 11:30:05. Unhealthy count fell 210 -> 93 -> 1. The saved budget shows 524 requests, zero strikes, no cooldown. All 209 available-stock cursors advanced to 11:20 or 11:25. Sector snapshots were received at 11:28:38 (11:25 board) and 11:30:20 (11:30 board), both with 29 valid sectors. ICICIPRULI still returned HTTP 200 with empty data on both Yahoo hosts; insurance remains unknown.

This is not evidence that the previous lock stall persisted. Pending fetches were submitted futures (including executor-queued jobs), not failed symbols or missed alerts. The session is meant to remain running, not exit when initial scanning completes. The divide warnings are numerical warnings from the unchanged shared scanner, not tracebacks terminating this session.

The state is cumulative across eleven starts. Of its 40 events, the latest four close events were discovered in catch-up at about 11:28 but refer to candles between 10:40 and 11:00; these are correctly non-live. No historical sector board can be invented, nor can stale events be resent as timely alerts. Historical running observations also do not establish that this new run observed those prices live. Attached older activity/PDF dates do not establish coverage for this run.

## Additional defects corrected

1. Empty-symbol recovery invalidated the entire main-process Yahoo cookie/session after 1d/5d empty responses. The recorded missing-stock diagnostics actually show this recovery action. One unsupported/missing symbol must not trigger reauthentication for healthy stocks. Removed this automatic empty-response invalidation. The unmodified shared fetcher already handles explicit 401/403 with session-owner-aware invalidation and one retry. The 1d -> 5d fallback, rate-limit safeguards and freshness validation remain.
2. Fetch admission submitted the whole universe into FIFO executor queues. Bound outstanding admissions to actual worker capacity (16 ordinary, 8 fast); retain separate lanes and oldest-request fairness within strategy priority. Track the actual executor lane per future even if a setup changes phase. Filter retry/poll-ineligible symbols before reserving slots. This removes hidden queue latency; it does not promise Yahoo publishes immediately.
3. The workflow's always-running handoff dispatched a successor after manual cancellation. Added !cancelled() to handoff only, retaining always-on diagnostics/state preservation. Natural shift completion and watchdog failure can still hand off under the existing cap. The existing 11/12 persisted start count is not reset or bypassed: this safety cap remains explicit. A fresh workflow cannot undo earlier session gaps.
4. Heartbeats now include budget status, sector health and a cursor-time histogram, so a growing request count and advancing analysis can be distinguished from persisted setup/event counts. Counts are plain JSON integers (covered by full runner session tests).

## Verification and limits

130 M16 tests passed, including real process/lock tests, delayed-sector/no-stale-close cases, missing-sector rank bounds, new healthy-session isolation and fetch admission/fairness regressions. Full M6 runner suite passed. M6 files, rules and routing are unchanged. Workflow shell syntax and cancellation condition are checked locally. No external Yahoo/Telegram execution is claimed; this sandbox cannot reach those hosts. Production deployment needs continuing heartbeats and fresh eligible observations. ICICIPRULI availability is still an upstream problem, not fixed by relabelling another ticker or fabricating insurance scores.
