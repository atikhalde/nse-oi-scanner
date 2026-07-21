# NSE paper-trading bot — Windows standby runner.
# Role: hot standby for the GitHub runner. If GitHub committed a live cycle
# recently (seen via the repo's PUBLIC commit feed), this exits quietly.
# If GitHub has been silent for 15+ min during market hours, this PC takes
# over: syncs the shared state, runs one full trade cycle, and pushes the
# state back so nobody alerts twice.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

# weekdays only (task scheduler runs daily; we self-skip Sunday=0/Saturday=6)
$dow = [int](Get-Date).DayOfWeek
if ($dow -eq 0 -or $dow -eq 6) { exit 0 }

# --- is GitHub alive? (public API, no auth, ~12 requests/hour used) ----------
$ghAlive = $false
try {
  $c = Invoke-RestMethod "https://api.github.com/repos/atikhalde/nse-oi-scanner/commits?per_page=5" `
         -Headers @{ "User-Agent" = "pc-standby" } -TimeoutSec 20
  foreach ($cm in $c) {
    if ($cm.commit.message -match "^cycle ") {
      $ageMin = ([DateTimeOffset]::UtcNow - [DateTimeOffset]$cm.commit.committer.date).TotalMinutes
      if ($ageMin -lt 15) { $ghAlive = $true }
      break
    }
  }
} catch { Write-Host "alive-check failed ($($_.Exception.Message)) — acting as standby-active" }

if ($ghAlive) { Write-Host "github alive — standby idle"; exit 0 }
Write-Host "github silent 15+ min — PC taking over this cycle"

# --- sync shared state (public repo: fetch/reset need no auth) ---------------
git fetch origin --quiet
git reset --hard origin/main --quiet

# --- one full trade cycle (secrets come from .env via live_runner itself) ----
& ".\.venv\Scripts\python.exe" live_runner.py --live

# --- publish state back so GitHub doesn't re-alert on re-entry ---------------
if (Test-Path "gh-token.txt") {
  $pat = (Get-Content "gh-token.txt" -Raw).Trim()
  git config user.name "pc-standby"
  git config user.email "pc@standby.local"
  git add -A
  git commit -m ("pcycle " + (Get-Date -Format "yyyy-MM-dd_HHmm")) 2>$null | Out-Null
  git push "https://x-access-token:$pat@github.com/atikhalde/nse-oi-scanner.git" 2>$null | Out-Null
}
exit 0
