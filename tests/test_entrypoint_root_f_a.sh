#!/usr/bin/env bash
# F-A live dry-run: ROOT entrypoint reaches the converge leg on beez.
# --dry-run keeps every action log-only; MNE_NO_SYSTEMD arms the systemd no-op.
set -uo pipefail
cd "$HOME/workspace/mnemo-pg/repo"
export MNE_NO_SYSTEMD=1
LOG="$HOME/.hermes/profiles/mike/cache/scratch/fa-dryrun.log"
REF="${1:-$(git rev-parse HEAD)}"
bash mnemo_update.sh --dry-run --ref "$REF" >"$LOG" 2>&1
rc=$?
echo "rc=$rc log=$LOG"
grep -nE "fetch source|deployed code|step 3.9|converge|ts-text|DRY\(sysd\)|RESULT" "$LOG" | head -30
exit $rc
