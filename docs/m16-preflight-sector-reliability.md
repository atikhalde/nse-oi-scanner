# M16 post-merge reliability preflight

## Findings and fixes

1. **Lost close on sector-worker race.** Previously a fresh technical close was permanently discarded when its sector board was not published yet. Fresh CLOSE_ONLY, CLOSE_CONFIRMED and DIRECT_BOS_CONFIRMED candidates now persist in the outbox while awaiting sector data. Delivery still requires a passing current board and the original 30-second freshness budget. A disqualified board rejects the candidate; a late board cannot revive it. Historical backfill and unknown-gate running crosses are never queued for retrospective delivery.
2. **Cold sector-history latency.** The dedicated sector process now primes history before the first candle close. Refresh overlaps Yahoo index, synthetic-member and history requests instead of fetching the groups serially. Missing index warm-up history fails closed instead of silently computing an unwarmed score.
3. **Dhan-capable shared basket path.** Sector basket fetching previously used M6's generic Yahoo-then-Dhan fallback helper. M16 sector members now explicitly use Yahoo only, irrespective of configured Dhan credentials. M6 itself is unchanged.

Delayed-close alerts preserve their original price-observation timestamp and explicitly display the later sector-eligibility verification time. Audit data preserves both gate decisions. JSON restart round-trips update the event journal and outbox copies consistently.

## Executed validation

- 107 M16 unit/integration regressions passed: sector, rally, calendar, feed, runner and audit.
- Full `python test_m6_runner.py` suite passed. Generated M6 test state/reports were restored/removed afterward.
- Workflow YAML parsing and every embedded shell block passed syntax checks.
- Python compilation and `git diff --check` passed.
- Deterministic dry-run scenarios: delayed passing board, board rejection, too-late board, restart recovery, duplicate suppression, unknown running gate and historical backfill.
- Two-bot delivery tested with mocked transports: one delivery per destination, no duplicate on the next flush.
- No real Telegram alerts or orders sent by these tests.

Reproduce offline tests:

```sh
python -m unittest -v test_m16_sector test_m16_rally test_m16_calendar test_m16_feed test_m16_runner test_m16_audit
python test_m6_runner.py
```

## External checks not certified

The read-only local `--feed-probe --symbols AXISBANK` failed with TLS EOF errors against both Yahoo hosts from this sandbox, whose outbound allowlist does not include Yahoo. This is not evidence of an outage on the GitHub runner.

A read-only GitHub `feed-probe` workflow dispatch was attempted but rejected with HTTP 403, Resource not accessible by integration. No workflow was started by that attempt. Live Yahoo availability on GitHub and Telegram acknowledgement have therefore not been certified by this preflight. An authorized Actions user can run workflow `16. LIVE M16` in `feed-probe` mode without sending Telegram or modifying live state; inspect its JSON `ok` results, not just workflow exit status.

## Remaining operating constraints

- A current complete sector board is required. Missing Yahoo components can intentionally block entries.
- An unknown-gate running breakout is not backdated when a future board arrives. A subsequent timely candle-close alert may still qualify.
- The 30-second close-delivery budget is not extended to hide delays.
- A one-second polling target is not exchange-to-Telegram latency or a guarantee that every transient cross is observed.
- The normal 09:00 IST supervised workflow, separate M16 bots, session-scoped notice deduplication, holiday rules and stale-alert rejection remain in place.
