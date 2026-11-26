# M16 live rate-limit incident: uploaded 26-Nov-2026 evidence

## Verified facts

Sources: user-uploaded `7_M16 dedicated session-5.txt` and `state16.json` (not committed).

- Runtime log contains 11,383 lines containing `HTTP 429`. This is a log-line count, not a deduplicated HTTP-request count.
- State date is 2026-11-26, session_starts=3; 209 stock cursors exist.
- Stock prices did recover: e.g. 360ONE/ABB show usable Yahoo 10:10 candles received around 10:15. ICICIPRULI remained unusable in that snapshot.
- Sector warm-up reports 64 symbol histories and 19 index histories loaded. Zero sector snapshots were accepted; latest saved reason is `sector board inputs stale/incomplete`.
- All 23 technical events have unknown sector eligibility and live=false. These are NOT 23 proven executable missed trades. The uploaded data cannot reconstruct their unavailable live sector eligibility.
- Saved degradation notice is complete with two acknowledged destination ledgers. Repeated user-visible notices cannot be attributed to a within-process deduplication bug from this evidence alone; multiple runs and two bot destinations must be distinguished.
- At snapshot time the old active-symbol definition included 55 symbols, but only 15 were armed/intrabar. The remaining rally setups did not need continuous running-price fetches.

## Defects addressed

1. Stock and sector/history requests previously had independent request-control paths. A stock process's 429 cooldown did not stop sector requests. Opt-in M16 transport now coordinates chart-call starts and cooldown through a flock-protected local file shared by the M16 parent and sector process. Real 429 callbacks establish a 30–300s escalating backoff, honoring longer Retry-After. A locally blocked call does not make a network request or extend the global penalty. Already in-flight requests cannot be recalled.
2. Rally setups and terminal audit symbols no longer receive unnecessary intrabar polling. Armed/intrabar setups retain a best-effort 1s scheduling target; other tracked symbols request missing completed candles. This does not change rally/pullback/BOS rules.
3. A nonempty response with no current-session candles no longer triggers an immediate wider-range request that cannot manufacture today's data. Failure evidence records source latest timestamp and expected session. Empty/transport failures retain the existing fallback behavior.
4. Sector input validation now names missing/stale symbols and their latest timestamps. Missing warm-up/current inputs still fail closed; no old board or partial ranking is silently substituted.
5. New `sector-probe` workflow mode exercises all board dependencies without Telegram or live-state writes. After hours it diagnoses the final 15:30 close. It exits nonzero on failure and is explicitly diagnostic, not a live eligibility decision.

## Configuration and tradeoffs

Workflow enables `M16_YAHOO_COORDINATE=1`, with a default minimum 0.125s between chart-call starts (at most approximately 8 starts/s within one M16 job). This is a configurable protective budget, NOT a published Yahoo allowance or a claim that rate limiting cannot recur. Each shared chart call can internally make auth/host retries; the coordinator does not count every HTTP handshake. Cookie/crumb handshakes and already-started requests are not retroactively blocked.

The limiter coordinates processes on the same host only. It cannot coordinate separate GitHub runners, other models, manual parallel jobs or external clients. The existing per-run stock cooldown remains as an additional guard. Shared `feeds.py`, M6 behavior, sector top3/>70, 30-second close freshness and no-backdating rules are unchanged.

With many armed stocks or slow responses, total request capacity may be lower than one update per symbol per second. Queueing/cooldown can delay or prevent a fresh alert. This patch protects recovery; it cannot truthfully guarantee all intrabar crosses.

## Validation

- 117 M16 tests passed, including cross-process file cooldown, Retry-After, escalating backoff, wrapper idempotence, default-off isolation, prior-session response handling and read-only full-board probe.
- Full `python test_m6_runner.py` passed; generated M6 state/reports restored/removed.
- Workflow YAML/shell syntax and diff checks passed.
- No real Telegram, orders or live-state mutations used for these tests.
- Actual GitHub/Yahoo full-board validation still required after deployment. Run `sector-probe`, inspect exact failing inputs if any, and inspect market-hours state/latency before claiming live recovery.
