#!/usr/bin/env bash
# Scan tracked files (and optionally a built bundle or other paths) for credential-like strings.
set -uo pipefail
cd "$(dirname "$0")/.."
PATTERNS='(sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|ghp_[A-Za-z0-9]{36}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer [A-Za-z0-9._-]{30,}|MODEL_API_KEY=[^ "$]{8,})'
hits=$(git ls-files -z | xargs -0 grep -nIE "$PATTERNS" 2>/dev/null | grep -v 'scripts/secret_scan.sh' | grep -v 'MODEL_API_KEY=delegate-model-api-key:latest' || true)
for extra in "$@"; do
  [ -e "$extra" ] && hits+=$(grep -rnIE "$PATTERNS" "$extra" 2>/dev/null || true)
done
KEYFILE="$HOME/.config/delegate/model_api_key"
if [ -s "$KEYFILE" ]; then
  k="$(tr -d '\r\n' < "$KEYFILE")"
  if git ls-files -z | xargs -0 grep -nF -- "$k" >/dev/null 2>&1; then hits+=$'\nTHE META KEY ITSELF APPEARS IN A TRACKED FILE'; fi
  for extra in "$@"; do [ -e "$extra" ] && grep -rqF -- "$k" "$extra" && hits+=$'\nTHE META KEY APPEARS IN '"$extra"; done
fi
if [ -n "$hits" ]; then echo "SECRET SCAN: FINDINGS"; echo "$hits" | sed -E 's/(.{0,40}).*/\1.../'; exit 1; fi
echo "SECRET SCAN: clean ($(git ls-files | wc -l) tracked files${*:+ plus $*})"
