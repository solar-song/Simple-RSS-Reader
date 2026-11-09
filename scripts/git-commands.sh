#!/usr/bin/env bash
# Minimal helper: stage everything, commit with provided message, and push to origin main.
set -euo pipefail
msg="$1"
if [ -z "$msg" ]; then
  echo "Usage: $0 \"commit message\""
  exit 2
fi
git add -A
git commit -m "$msg"
git push -u origin main
