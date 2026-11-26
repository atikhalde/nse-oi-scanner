# Missing ICICIPRULI: prevent a single basket failure from blocking all M16 sectors

## Uploaded probe evidence

`7_M16 dedicated session-6.txt`: at 2026-11-26 10:45 IST, Yahoo returned empty responses for ICICIPRULI on both hosts. The sector probe failed with:

`sector board incomplete for 10:40: ICICIPRULI:latest=none`

This identifies the blocker precisely. This probe does not show a new HTTP 429. The prior request-coordination fix does not make a genuinely unavailable stock return data.

## Conservative fault isolation

- Global inputs (current NIFTY, VIX, official sector indices and index warm-up) remain mandatory.
- Any missing/stale synthetic member or unavailable member history marks its WHOLE home sector unknown. No stale member price, replacement ticker, smaller reweighted basket, or partial-sector score is invented.
- Remove all members of that sector from the current board computation. Other sectors retain their existing engine-derived scores. A regression exercises the real flow engine to verify score independence.
- If `m` sectors are unknown and the stock's known-sector rank is `r`, entry requires **r + m <= 3** AND directional persistence **strictly >70**.
- With INSURANCE unknown: known ranks 1/2 can safely qualify; known rank 3 remains unknown, because insurance might outrank it. All insurance entries remain unknown.
- More unknown sectors tighten the bound further. No missing sector is silently assigned a zero score. A complete board retains the original top-three rule.
- Incomplete prior-day basket caches are discarded so recovery cannot reuse an accidentally partial baseline.

Snapshots expose `complete`, `missing_sectors`, `missing_members`, and `rank_policy`; decisions expose known/worst-case rank. Probe JSON labels degraded coverage. Passing alerts disclose incomplete coverage and the guaranteed worst-case rank. Session-scoped health-notice deduplication remains unchanged.

## What does not change

M6 source/behavior, top-three threshold, persistence >70, timing cutoffs, freshness budgets, no-backdating, Yahoo-only fetching and rate-limit coordination. This is fault isolation under a mathematical ranking bound, not removal of the persistence gate. Recovery uses a later complete fresh board; previously rejected/expired signals are not replayed.

The normal refresh retries missing inputs on subsequent candle-board refreshes. This fix does not repair Yahoo's ICICIPRULI data or prove a complete 30-sector board is healthy.

## Validation

- 122 M16 tests passed, including the exact missing-ICICIPRULI case, exclusion of the whole insurance basket, uncertain third-rank rejection, worst-case-rank safety over possible missing-sector scores, full-board behavior and real-engine score independence.
- Full M6 runner suite passed; generated test state and reports cleaned up.
- No live Telegram sends or production-state writes used for validation.

After deployment, run `sector-probe` on main. A usable 29-sector result must explicitly show `complete: false`, `coverage_status: degraded`, `missing_sectors: [INSURANCE]`, and `missing_members: [ICICIPRULI]` if the upstream data is still absent. Do not interpret a usable degraded board as complete provider recovery. Existing running sessions need a controlled restart to load merged code.
