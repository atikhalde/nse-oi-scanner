#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# One-command installer for the NSE paper-trading bot on any Ubuntu/Debian VM
# (Oracle Always Free Tier recommended).
#
# Run ON the VM (after you have its SSH shell):
#   curl -sL https://raw.githubusercontent.com/atikhalde/nse-oi-scanner/main/vm-setup.sh | bash
#
# What it does:
#   1. installs python venv + git + cron
#   2. clones your PUBLIC repo (read-only pulls forever - no tokens needed)
#   3. installs pinned python deps (pandas 2.2.3 etc.)
#   4. creates run-live.sh / run-bootstrap.sh wrappers (env from ./.env)
#   5. installs two cron lines (UTC clock → IST noted in comments)
#   6. runs the engine self-test so you instantly see PASS/FAIL
#
# After it finishes (2-3 min): create your secrets file once —
#   cd ~/nse-oi-scanner && cp .env.example .env && nano .env
# ─────────────────────────────────────────────────────────────────────────────
set -e
REPO="https://github.com/atikhalde/nse-oi-scanner.git"
DIR="$HOME/nse-oi-scanner"

echo "== packages =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git curl

echo "== clone/pull code =="
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only || true
else git clone "$REPO" "$DIR"; fi
cd "$DIR"

echo "== python venv + deps =="
python3 -m venv .venv
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo "== folders =="
mkdir -p logs data/history

echo "== wrappers =="
cat > run-live.sh <<'SH'
#!/usr/bin/env bash
cd "$(dirname "$0")"
set -a; [ -f .env ] && . ./.env; set +a
. .venv/bin/activate
flock -n .lock-live python3 live_runner.py --live >> logs/live.log 2>&1   # never overlaps itself
SH

cat > run-bootstrap.sh <<'SH'
#!/usr/bin/env bash
cd "$(dirname "$0")"
git pull --ff-only || true          # pick up code updates once a day
set -a; [ -f .env ] && . ./.env; set +a
. .venv/bin/activate
flock -n .lock-boot python3 live_runner.py --bootstrap refresh-60d >> logs/bootstrap.log 2>&1
SH
chmod +x run-live.sh run-bootstrap.sh

echo "== crontab (VM clock is UTC; IST noted) =="
( crontab -l 2>/dev/null | grep -v "nse-oi-scanner/run-" ; \
  echo "# 08:45 IST daily: refresh history + prev-day OI base" ; \
  echo "45 3 * * 1-5 $DIR/run-bootstrap.sh" ; \
  echo "# every 5 min, 08:30-16:25 IST: scan/gate/trade/alert (engine idles off-hours)" ; \
  echo "*/5 3-10 * * 1-5 $DIR/run-live.sh" ) | crontab -
sudo systemctl enable --now cron >/dev/null 2>&1 || sudo service cron start || true

echo "== engine self-test =="
python3 test_master_scanner.py | tail -2 || true

echo
echo "───────────────────────────────────────────────────────────"
echo "SETUP DONE ✅  — two things left:"
echo "  1) secrets :  cp .env.example .env && nano .env   (fill 3 values, Ctrl+O Enter Ctrl+X)"
echo "  2) smoke   :  ./run-live.sh && tail -5 logs/live.log"
echo "Then forget it — cron runs every 5 min on its own."
echo "Logs:  tail -f ~/nse-oi-scanner/logs/live.log"
echo "───────────────────────────────────────────────────────────"
