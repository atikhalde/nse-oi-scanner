# M12/M13 Dhan 429 fast-fallback fix

## Behavior

- M12/M13 try Dhan at the start of a cycle when a Dhan token exists.
- The first Dhan failure, including HTTP 429, opens a circuit breaker for the rest of that cycle.
- Every remaining symbol goes directly to Yahoo Finance.
- Yahoo receives one immediate request only: no retry loop and no sleep.
- The old 0.15-second per-symbol M12/M13 sleep is removed.
- One fallback line is logged per cycle instead of 210 Dhan error lines.
- Feed counters and the fallback reason are persisted in `state12.json` / `state13.json` under `feed`.

## Upload

Upload these root files:

- `fast_feed.py`
- `test_fast_feed.py`
- `workflow_safe_push.sh`
- `m12_runner.py`
- `m13_runner.py`

Upload these workflow files into `.github/workflows/`:

- `12_live_m12.yml`
- `13_live_m13.yml`

## Verify

Run each workflow once. A Dhan 429 should produce one line similar to:

`FAST-FEED: Dhan disabled for this cycle (HTTP 429); switching immediately to Yahoo`

State should show approximately:

```json
"feed": {
  "dhan_calls": 1,
  "yahoo_calls": 210,
  "fallback": "HTTP 429",
  "fed": 210
}
```

Actual Yahoo coverage can be lower if an individual Yahoo request fails. Such failures are not retried in the same cycle.
