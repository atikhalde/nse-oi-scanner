#!/usr/bin/env bash
# Rebase-and-retry push helper for concurrent paper workflows.
set -euo pipefail

for attempt in 1 2 3 4 5 6; do
  echo "safe push attempt ${attempt}/6"
  if ! git pull --rebase origin main; then
    echo "rebase conflict; aborting rather than corrupting state" >&2
    git rebase --abort || true
    exit 1
  fi
  if git push origin HEAD:main; then
    echo "safe push succeeded"
    exit 0
  fi
  sleep $((attempt * 5))
done

echo "safe push failed after 6 rebased attempts" >&2
exit 1
