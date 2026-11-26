# Manual recovery after automatic restart exhaustion

Uploaded session-10 exits with `M16 supervised restart cap reached` at 11:43:26 IST, before live_session is entered. The handoff also declines another run. This is a deterministic startup guard failure, not evidence of another Yahoo/worker failure. Legacy state has exhausted twelve total starts.

## Correction

- A manually dispatched session defaults to manual recovery (`automatic_handoff` unchecked). It can start even when the automatic allowance is exhausted.
- Scheduled sessions and workflow-created successors explicitly run as automatic. Successors pass `automatic_handoff=true`; this prevents an automated workflow_dispatch from masquerading as an operator restart.
- Track total starts and automatic starts separately. Manual starts do not consume automatic allowance or replenish it. Legacy state migrates conservatively: the old total is treated as already consumed automatic allowance. At the legacy cap, a manual session runs but no automatic successor is dispatched that day.
- CLI starts default to automatic (fail-safe). An operator may explicitly set M16_AUTOMATIC_SESSION=0 for manual recovery outside the workflow.
- Keep the daily cap of twelve automatic starts and the existing no-handoff-on-cancellation condition. A new trading date uses fresh state as before.
- Preserve all setups, cursors, event/delivery deduplication, freshness rules, missing-insurance policy and bots. Do not delete or edit production state to work around the guard. No M6 changes.

## Operator action

After deployment select workflow `16. LIVE M16`, branch main, mode session; leave `automatic_handoff` unchecked. The startup log should identify a manual recovery session, total starts and used automatic allowance. The run then monitors for up to 225 minutes or session close, whichever comes first. If the daily automatic cap was already consumed and the manual shift ends before close, another explicit operator start is required. Previously missed observations are not resent as fresh alerts.

## Verification

132 M16 tests passed, including reproduction of the exact legacy twelve-start guard, manual recovery without changing events/cursors, unchanged automatic rejection, and manual/automatic counter separation. Workflow YAML, shell syntax, explicit successor input and start-mode wiring are checked locally. This is not a live Yahoo/Telegram certification.
