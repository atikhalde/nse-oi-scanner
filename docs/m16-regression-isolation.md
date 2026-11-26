# Regression-step failure and state recovery (session 11)

The uploaded offline-regression log fails test_supervised_session_restart_cap_prevents_another_session: SystemExit was not raised. The test inherited M16_AUTOMATIC_SESSION=0 from the manual workflow. Its mocked load returned a synthetic 2026-11-25 state, but save was not mocked, so main wrote that fixture to the real checkout. The always-running preservation step committed it; handoff then dispatched a successor. The live session never began. Uploaded state16-7.json and remote main both confirm the synthetic date with zero events/cursors.

Correction:
- Automatic-cap CLI test explicitly selects automatic mode and mocks save; assert neither save nor live monitoring runs when capped.
- A separate manual-recovery CLI regression explicitly selects manual mode and mocks persistence; assert manual recovery runs at the cap.
- Workflow tests execute twice, under both startup environments, in a disposable git-archive copy. The real checkout's state/reports cannot be changed by fixtures even if future tests regress. Dedicated bot credentials are blank in the test step.
- Automatic handoff requires successful regressions; cancellation guard remains. A test failure no longer launches another failing chain.
- Restored state16.json byte-for-byte from the user's state16-6.json: 2026-11-26, 42 events, 209 cursors, twelve total starts. This is the pre-corruption snapshot, not fabricated recovery data. Manual recovery remains available; automated restart budget is not reset. No missed observations or delivery acknowledgements are invented.

Validation: 133 M16 tests passed under M16_AUTOMATIC_SESSION=0 and again under =1, with coordination disabled as in workflow tests. Workflow shell syntax passed. Disposable-copy execution is checked separately. No M6 runtime/strategy changes. External live Yahoo/Telegram operation is not certified by these offline regressions.
