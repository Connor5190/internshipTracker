#!/usr/bin/env bash
# Commit and push the ledger files a scan just updated.
#
# Both workflows write the same ledger, so a push can lose a race with the
# other one. Rebase and retry rather than dropping the run's first-seen
# dates, which can't be reconstructed after the fact.
set -euo pipefail

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git add .state/seen_postings.json .state/companies_seen.json
if git diff --staged --quiet; then
  echo "Ledger unchanged."
  exit 0
fi
git commit -m "Update seen-postings ledger [skip ci]"
for attempt in 1 2 3; do
  if git push; then exit 0; fi
  echo "Push lost a race — rebasing (attempt $attempt)…"
  git pull --rebase
done
git push
