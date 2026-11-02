#!/usr/bin/env bash
# Clean-clone release check: fresh clone of a commit, frozen installs, builds, type checks, all tests, secret scan.
# Usage: scripts/release_check.sh [git-ref]   (writes evidence/test-runs/clean_clone_<sha>.txt in the source repo)
set -uo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
REF="${1:-HEAD}"
SHA="$(git -C "$SRC" rev-parse --short=12 "$REF")"
DIR="$(mktemp -d /tmp/delegate-clean-XXXX)"
OUT="$SRC/evidence/test-runs/clean_clone_${SHA}.txt"
step() { local name="$1"; shift; local t0=$SECONDS; if "$@" >"$DIR/step.log" 2>&1; then echo "PASS $name ($((SECONDS-t0)) s)"; else echo "FAIL $name ($((SECONDS-t0)) s)"; tail -20 "$DIR/step.log"; FAILED=1; fi; }
FAILED=0
{
  echo "Clean-clone release check for $SHA at $(date -u +%FT%TZ) on $(uname -sr), python $(python3.12 --version 2>&1), node $(node --version)"
  step "clone" git clone -q "$SRC" "$DIR/repo"
  cd "$DIR/repo" && git checkout -q "$SHA"
  step "uv sync --frozen" uv sync --frozen --python /usr/bin/python3.12
  step "npm ci" sh -c 'cd apps/web && npm ci --no-audit --no-fund'
  step "web typecheck" sh -c 'cd apps/web && npm run typecheck'
  step "web build" sh -c 'cd apps/web && npm run build'
  step "mypy --strict" uv run mypy
  step "ruff" uv run ruff check services packages tests
  step "pytest (all Python tests)" sh -c 'uv run pytest -q -p no:warnings --junitxml=pytest.xml && python3 -c "import xml.etree.ElementTree as E;r=E.parse(\"pytest.xml\").getroot();s=r if r.tag==\"testsuite\" else r.find(\"testsuite\");print({k:s.attrib[k] for k in (\"tests\",\"failures\",\"errors\",\"skipped\")})"'
  grep -h "tests" "$DIR/step.log" | tail -1
  step "gate harness (151 cases)" uv run python evaluation/gate_harness.py
  step "secret scan" scripts/secret_scan.sh apps/web/dist
  echo "RESULT: $([ $FAILED = 0 ] && echo PASS || echo FAIL)"
} | tee "$OUT"
rm -rf "$DIR"
exit $FAILED
