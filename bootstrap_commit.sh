#!/usr/bin/env bash
# Commit-and-push one bootstrap stage.
#
# Bootstrap used to run its three stages (history refresh -> m9 farm -> ML
# labels) and commit once at the very end. When the slow optional stages pushed
# the job past its timeout, GitHub cancelled the run and *nothing* was
# committed — including the 210-symbol history refresh that every M12/M13/M14
# previous-close gate depends on. That is what froze data/history at 2026-08-18
# and left all three models STALE.
#
# Each stage now commits immediately after it finishes, so a later cancellation
# can never undo an earlier success.
set -euo pipefail

MSG="${1:-bootstrap stage}"

git config user.name "paper-bot"
git config user.email "paper-bot@users.noreply.github.com"
git add data/ learn/

if git diff --cached --quiet; then
  echo "bootstrap stage: nothing to commit ($MSG)"
  exit 0
fi

git commit -m "$MSG"

# Live paper workflows may advance main while a stage is running.
if ! git pull --rebase origin main; then
  echo "rebase conflict — aborting rather than corrupting state" >&2
  git rebase --abort || true
  exit 1
fi

bash workflow_safe_push.sh
