M11 TEST-ALERT PATCH — upload + how to fire the test
====================================================

STEP 1 — repo ROOT → Add file → Upload files (2 files):
    m11_runner.py          afcdeb6ec55b0833f82fc64c5c19ba10
    test_m11_runner.py     b1f33f0f725ae3945d6590f8f3b0e034

STEP 2 — go INTO .github/workflows/ → Upload files (1 file):
    workflows/11_live_m11.yml     55e55bc900f726cb6e0f1b75cca95db2

STEP 3 — tell me "done" → I verify the 3 md5s on the repo.

FIRE THE TEST ALERT (any time, ~2 min):
    GitHub repo → Actions tab → left list click
       "11. LIVE M11 — master × video-4 alignment (paper-only)"
    → top-right "Run workflow" → in the "mode" box change `live`
       to:  test-alert
    → green "Run workflow" button.
    Wait 1–3 minutes. You should receive TWO messages in the main chat
    AND in BOTH new bot chats (if the 4 M11_* secrets are set):
       🧪 🅼11 TEST — hello from the M11 lab …
       🅼11 · 🚨 ENTRY · KAYNES 🟢 BUY · BUY-EX17  (sample alert, shows
       Setup name / SL / 🎯 TARGET / 🎬 video setup names)
    The workflow log prints: "M11 test alert dispatched to N target(s):
    main=yes extras=2" — send me that line if you want me to double-check.

SAFE: cron + schedule runs stay in live mode (default), nothing to undo.
The test mode never touches trades or state11.json.
