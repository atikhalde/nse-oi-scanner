# M16 persistent empty-symbol backoff

## Evidence and scope

The uploaded dedicated session-12 continued minute heartbeats through 12:29:57 IST, progressed to the current completed candle and obtained a 12:25 sector board at 12:25:22. Budget requests increased without recorded 429 strikes. ICICIPRULI repeatedly returned HTTP 200 with no timestamps; it was the only affected stock. The log ends with external cancellation, not a scanner traceback. This patch addresses the repeated empty-response load/logging, not a claimed total scanner failure or restored Yahoo data.

## Behaviour

The opt-in M16 transport now shares a per-symbol empty-data circuit across stock and sector processes through the existing bounded-lock budget file. Only 5-minute 1d/5d chart calls participate. The first empty call still allows the fallback range. After two all-empty calls, repeated attempts for that symbol return explicit empty-backoff diagnostics without network requests. Retry delays increase 30, 60, 120, 240, then 300 seconds maximum. Other symbols and historical ranges are not blocked. A successful response clears that symbol's circuit and logs recovery. This is a temporary negative circuit, never cached usable data.

M16 coalesces the shared fetcher's repeated per-host empty lines into a summary of fallback checking / retry delay. Other error messages (including auth and rate limits) remain visible. Heartbeat budget diagnostics include currently backed-off symbols and seconds remaining. Missing-member/sector handling, strict freshness and rank bounds are unchanged; ICICIPRULI and insurance cannot become valid merely because logging is quieter.

The shared fetcher has an optional log callback; default callers still use print and retain their original fetch/retry behaviour. M6 never installs the M16 transport, so its polling, logs and circuit behaviour are unchanged. No global stdout redirection or print monkey-patching is used at runtime.

## Verification

136 M16 tests pass, including real shared-fetcher empty HTTP 200 responses, default logging versus M16 coalescing, per-symbol isolation, first range fallback, real second-process visibility, expiry/recovery, capped exponential backoff, history exemption and existing 429/cross-process-lock regressions. Full M6 runner suite passes. Tests are also executed in disposable copies under both workflow startup environments; production state is not changed.

The latest upload ran before this patch was merged; it cannot be evidence that this backoff failed. Existing runners do not hot-load code. No live Yahoo or Telegram certification is claimed, nor is upstream ICICIPRULI availability fixed. No further upload of the same host-empty messages is needed to establish this specific defect.
