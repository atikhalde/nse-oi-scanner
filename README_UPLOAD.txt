ALL-IN-ONE UPLOAD: M10 (Coach-v2.2) + M11 (Master×Video-4, M8-spec alerts) + trader.py
Repo checked 02-Aug night: M10/M11 files ABSENT; trader.py OLD; engine intact.
====================================================================================

STEP 1 — upload these 5 files at repo ROOT (root page → Add file → Upload files)
    m10_runner.py          (new  — Coach-v2.2 entry lab)
    test_m10_runner.py     (new)
    m11_runner.py          (new  — video-4 alignment lab)
    test_m11_runner.py     (new)
    trader.py              (RE-UPLOAD — 1-line alert change; output identical for
                            M1–M10, M9/M10/M11 suites all pass; safe for all models)

STEP 2 — go INTO .github/workflows/ (click .github → workflows → Add file → Upload files)
    workflows/10_live_m10.yml
    workflows/11_live_m11.yml

STEP 3 — Telegram: create 2 NEW bots (@BotFather → /newbot ×2), open each, press Start

STEP 4 — Repo → Settings → Secrets and variables → Actions → 4 NEW secrets
    M11_BOT_TOKEN_A  M11_CHAT_ID_A  M11_BOT_TOKEN_B  M11_CHAT_ID_B
    (never paste tokens in chat/issues/code)

STEP 5 — cron-job.org: clone the M8 trigger TWICE →
    trigger A → .../actions/workflows/10_live_m10.yml/dispatches
    trigger B → .../actions/workflows/11_live_m11.yml/dispatches

STEP 6 — tell me "done" → I verify these md5s + 13 workflows registered:
    m10_runner.py          c76ed7a1114f434918ada131de488a62
    test_m10_runner.py     30926ee7ebb8bdd2ffc361b27e3ef450
    m11_runner.py          8d55f6a5b9e3d687cb663bc04f897ced
    test_m11_runner.py     bb38439b857b3ba20a5e477578b1b245
    trader.py              df1bf9f201e77c0460a937b6a3c694a2
    10_live_m10.yml        67da060bd72df5b890331e635104e33c
    11_live_m11.yml        7c2d7f51c0c7a07fa819028d48b86dfb
    master_scanner.py      3db1a09c01d7c1a429d3697761a12342  (must stay this)

Monday 03-Aug morning (before 06:10 Bahrain): fresh DHAN_TOKEN secret (daily ritual).
