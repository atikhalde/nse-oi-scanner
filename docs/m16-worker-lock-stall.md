# M16 fetch/worker lock stall hardening

## Uploaded evidence

The uploaded `7_M16 dedicated session-8.txt` prints one heartbeat at 11:05:48 IST with 210 pending fetches, 0 pending engines and 210 unhealthy stocks, followed by manual cancellation approximately five minutes later. `state16-4.json` contains a Yahoo budget with requests_started=1, no recorded cooldown, and old sector-health/cursor data. Old persisted fields must not be mistaken for successful checks by the new process. The live session is intentionally long-running; the loss of progress, not simply the job remaining active, is the incident.

This evidence is consistent with a synchronization stall, not sufficient on its own to prove a particular blocked stack. Code review found a reproducible inherited-file-descriptor lock hazard: Linux's default fork workers can start lazily while another fetch thread holds the shared budget file lock. The child inherits the open-file description; merely closing the parent's file does not release the flock while the child retains that reference. Subsequent fetches can block, followed by the heartbeat's own budget read. Forking while shared Python/network locks are held is also unsafe.

## Fix

- All three M16 process pools explicitly use the spawn context. Analysis/sector children start with independent interpreter/lock/session state rather than copying threaded parent locks and descriptors.
- Budget updates explicitly issue LOCK_UN in finally, even when the callback fails. They no longer rely solely on close() to release a potentially inherited lock.
- Lock acquisition is nonblocking with a two-second deadline. A lock failure becomes a diagnostic exception rather than an indefinite fetch wait.
- Heartbeat budget status catches the bounded lock timeout and exposes an error instead of hanging or terminating the main loop.

No change to M6, signal rules, Yahoo provider, 429 backoff, top-three bounds, missing-insurance handling, freshness or Telegram destinations.

## Validation

127 M16 tests passed. Added real subprocess tests exercise an alive forked child retaining the parent's fd, a real spawned worker accessing the budget after the parent's lock closes, bounded lock waits and exception cleanup. The runner regression asserts all three process pools select spawn. Existing delayed-close, cooldown, sector and delivery regressions also pass.

The initial new startup assertion was invalid: submitting the bound DefaultContext.get_start_method method pickled the parent's context. Resolving the child's own start method in a top-level worker confirms spawn and passes. This was a test-instrumentation error, not a reason to revert process isolation.

The full M6 runner suite passed; generated M6 state/reports were restored or removed afterward. No live Telegram messages, orders or production-state changes were used for these tests.

## Deployment verification

Existing processes cannot hot-load this change. A new live session must show continuing heartbeats, a growing request counter, current cursors and received sector snapshots. A successful offline test or isolated sector probe is not full sustained-live certification. External provider outages, request-capacity limits and scheduler delays remain possible; no no-missed-alert guarantee is made.
