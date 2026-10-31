#!/usr/bin/env bash
# mnemo_update.sh (scripts/ path) — COMPATIBILITY WRAPPER, zero logic.
#
# F-A (2026-10-31): this used to be a second, divergent deploy pipeline
# (dev-checkout variant with converge legs 5-5g the root script never ran;
# the drift caused the btrim(timestamptz) write-crash class on hosts
# deployed via the root entrypoint, e.g. mybuddy-template). ONE source of
# truth now: the root mnemo_update.sh. Do NOT add logic here.
#
# Legacy invocations stay valid:
#   bash ~/mnemo_update.sh [--repo <path>] [--no-restart]  ->  ~/mnemo_update.sh
#       exec-chains into <repo>/scripts/mnemo_update.sh -> here -> repo root
#   bash ~/.hermes/plugins/mnemo-pg/mnemo_update.sh [--ref R] [--dry-run] [--no-restart]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/mnemo_update.sh"
[ -f "$ROOT" ] || { echo "mnemo_update: root script not found at $ROOT" >&2; exit 2; }

# translate the wrapper-era flags into the root CLI (--repo has no root
# equivalent: the root script discovers its source itself — plugin dir,
# dev checkout, or fresh clone).
ARGS=()
skip=0
for a in "$@"; do
  if [ "$skip" = 1 ]; then skip=0; continue; fi
  case "$a" in
    --repo) skip=1;;
    *) ARGS+=("$a");;
  esac
done
exec bash "$ROOT" "${ARGS[@]}"
