#!/usr/bin/env bash
# Scan a repository's full git history for credentials before publishing it.
# Usage: tools/secret_scan.sh [path-to-repo]
# Prefer gitleaks when installed; this grep pass is the quick fallback used
# on the production repository before this showcase was published.
set -euo pipefail
repo="${1:-.}"
cd "$repo"

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --source . -v
  exit $?
fi

patterns='AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY|xox[baprs]-[0-9A-Za-z-]+|ghp_[A-Za-z0-9]{30,}|(password|passwd|secret)[A-Za-z_]*[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"'[:space:]]{6,}["'"'"']'

hits=$(git log -p --all | grep -niE "$patterns" \
  | grep -viE 'password_fail|make_password|set_password|check_password|getenv|environ|placeholder|help_text|verbose_name|def ' || true)

echo "== possible secrets in history =="
echo "${hits:-none}"
echo "== sensitive files ever committed =="
git log --all --name-only --format= | grep -E '\.(sql|sqlite3)$|(^|/)\.env' | sort -u || echo none

[ -z "$hits" ]
