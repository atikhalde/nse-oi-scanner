=========================================================================
UPLOAD THESE FILES TO GITHUB  (repo: atikhalde/nse-oi-scanner)  · 24-Jul-2026
=========================================================================

ROOT FILES  →  upload to the repo ROOT folder (13 files):

  trader.py            exits v3: structure SL, NO fixed targets, +1R trail
  report.py            reports with Costs + Net P&L columns, MIS 5x note
  costs.py             brokerage/STT/fees engine (July-2026 rates)
  options_common.py    options engine (1st ITM, nearest expiry, risk caps)
  live_runner.py       M1  (stocks, top-30 OI spurt)   + 09:26 + B2 filter
  m2_runner.py         M2  (stocks, any spurt x movers) + 09:26 + B2 filter
  m3_runner.py         M3  (OPTIONS mirror of M1)   NEW
  m4_runner.py         M4  (OPTIONS mirror of M2)   NEW
  test_trader.py       updated tests for exits v3
  test_options.py      options engine tests        NEW
  OPTIONS_MODEL.md     options rules doc           NEW
  EXIT_ENGINE_V2.md    exit rules doc (updated with B2 filter)
  master_scanner.py    THE ENGINE — unchanged, upload not needed
                       (included only as a safety copy; GitHub copy is
                        already identical)

WORKFLOW FILES  →  upload into  .github/workflows  (2 files):

  .github/workflows/5_live_m3.yml   NEW  (M3 options live loop)
  .github/workflows/6_live_m4.yml   NEW  (M4 options live loop)

  (1_test.yml / 2_bootstrap.yml / 3_live.yml / 4_live_m2.yml are unchanged —
   included only as reference copies, no need to touch them on GitHub.)

HOW TO UPLOAD (web browser):
  1. Open the repo page -> "Add file" -> "Upload files".
  2. Drag the 12 root .py/.md files in -> "Commit changes".
  3. Navigate into .github/workflows (or use "Add file" -> "Create new file"
     with the name typed as  .github/workflows/5_live_m3.yml  — typing "/"
     auto-creates folders) and add 5_live_m3.yml, then 6_live_m4.yml.

AFTER UPLOAD — 60-second verification on the GitHub website:
  Open each file, press Ctrl+F, confirm the search string is found:

  trader.py          ->  TP1_R, TP1_FRAC = 99.0
  report.py          ->  MIS 5×
  live_runner.py     ->  EX_WEAK_CODES = {102}
  m2_runner.py       ->  EX_WEAK_CODES = {102}
  costs.py           ->  STOCK_SLIP = dict(base=0.0002
  options_common.py  ->  OPT_RISK_CAP = 1900

  If all six are found, the upload is complete and correct.
Then set up the two cron-job.org triggers for M3/M4 (see CRON_SETUP_M3_M4.txt).
