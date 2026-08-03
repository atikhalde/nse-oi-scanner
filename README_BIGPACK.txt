BIG PACK — no-dup alerts (7 models) + hourly heartbeats + M9 removal
=====================================================================

STEP 1 — repo ROOT → Add file → Upload files (7 files):
    live_runner.py  (M1)
    m2_runner.py    (M2)
    m5_runner.py    (M5)
    m6_runner.py    (M6)
    m7_runner.py    (M7)
    m8_runner.py    (M8)
    m10_runner.py   (M10)
    (m11_runner.py NOT in this pack — already updated yesterday, untouched now)

STEP 2 — go INTO .github/workflows/ → Upload files (1 file):
    workflows/0_cleanup_m9.yml

STEP 3 — FIRE the M9 removal (one click):
    Repo → Actions → left list: "0. ONE-SHOT — REMOVE M9 completely"
    → Run workflow → Run workflow (green). Wait ~1 min. It deletes:
      m9_runner.py, m9_bootstrap.py, test_m9_runner.py, m9_universe.csv,
      state9a/b/c.json, data/history9 (~300MB, 1,050 files), workflows
      9a/9b/9c — AND deletes ITSELF. Learn rows (learn/raw_M9*.csv) KEPT
      on purpose (small, already-harvested ML data).
    Workflows list after: 10 workflows (1,2,3,4,5,6,7,8,10,11).

STEP 4 — cron-job.org → DELETE the 3 M9 triggers (9a, 9b, 9c dispatches).
    (If left on, they just hit a dead URL — harmless but noisy.)

STEP 5 — tell me "done" → I verify: 7 runner md5s + M9 files 404 +
    10 workflows + engine 3db1a09c.

What changed in alerts (all models, M9 gone):
    * Same alert can never fire TWICE: every model now registers the alert
      + saves state BEFORE sending (crash/resume immune). Dedup keys
      (tkey:EVENT) already existed — nothing else changed.
    * Heartbeats now 1x/hour instead of every 15 min (still silent; still
      shows in workflow logs every cycle). Trade alerts unchanged:
      ENTRY/TP/SL/EOD alerts all keep firing exactly as before.
    * M11 already had all this from yesterday's 1a/2b/3b patch.
