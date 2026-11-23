#!/usr/bin/env bash
# Commit data/ and push to main, always rebasing on the latest main first (SPEC §7, CLAUDE.md "Git").
# Usage: bash scripts/commit_data.sh "commit message"
set -euo pipefail

message="$1"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add data/
if git diff --staged --quiet; then
  echo "No data changes to commit."
  exit 0
fi
git commit -m "$message"

for attempt in 1 2 3 4 5; do
  if git pull --rebase origin main && git push origin HEAD:main; then
    echo "Pushed on attempt $attempt."
    exit 0
  fi
  echo "Push failed (attempt $attempt), retrying..."
  sleep $((attempt * 5))
done

echo "Could not push after 5 attempts." >&2
exit 1
