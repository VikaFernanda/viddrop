#!/usr/bin/env bash
# Block commits that contain sensitive files or credential patterns.

set -e

STAGED=$(git diff --cached --name-only)

# Block sensitive file types
if echo "$STAGED" | grep -qE '\.(env|key|pem)$|secrets\.json|credentials\.json|\.netrc'; then
  echo "BLOCKED: attempt to commit sensitive file(s)."
  echo "Files matched: $(echo "$STAGED" | grep -E '\.(env|key|pem)$|secrets\.json|credentials\.json|\.netrc')"
  exit 1
fi

# Block hardcoded passwords/tokens in Python files
if git diff --cached -U0 -- '*.py' | grep -qiE '(password|token|secret|api_key)\s*=\s*["\x27][^"\x27]{4,}'; then
  echo "BLOCKED: potential hardcoded credential detected in staged Python files."
  echo "Use credential_store.py and keyring instead."
  exit 1
fi

echo "Pre-commit checks passed."
exit 0
