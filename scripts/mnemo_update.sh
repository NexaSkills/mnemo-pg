#!/usr/bin/env bash
# mnemo-pg host deploy — the ONLY sanctioned way to update a host's
# mnemo-pg runtime (this host included). Direct psql/ALTER fixes on any
# host are forbidden: codify them as scripts here, deploy via git.
#
# Usage:  bash ~/mnemo_update.sh [--repo <path>] [--no-restart]
# Same detached-launch discipline as lcm_update.sh: NEVER run foreground
# from a session served by the surfaces it restarts.
#
# Order is load-bearing:
#   1 git pull          code first (self-heal/adapter fixes precede DDL)
#   2 plugin links      root + every profile -> repo/plugin
#   3 provision         schemas to the native_ddl contract (idempotent)
#   4 lineage fix       canonical_facts PK/defaults (codified repairs)
#   5 converge          core-contract columns/defaults/shadow drop
#   6 live_route        per-profile PG-route verdict
#   7 restart surfaces  unless --no-restart
set -euo pipefail

REPO="${MNE_REPO:-$HOME/workspace/mnemo-pg/repo}"
VENV="${MNE_VENV:-$HOME/lcm-pg/venv}"
PY="$VENV/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
RESTART=1

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --no-restart) RESTART=0; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }

log "step 1: git pull ($REPO)"
git -C "$REPO" pull --ff-only origin main

log "step 2: plugin symlinks -> $REPO/plugin"
mkdir -p "$HOME/.hermes/plugins"
ln -sfn "$REPO/plugin" "$HOME/.hermes/plugins/mnemosyne"
for pdir in "$HOME"/.hermes/profiles/*/; do
  [ -d "$pdir" ] || continue
  p="$(basename "$pdir")"
  # quarantine_legacy_plugin.py's removal is idempotent; re-link after
  [ -e "$pdir/plugins/mnemosyne" ] && rm -rf "$pdir/plugins/mnemosyne"
  mkdir -p "$pdir/plugins"
  ln -sfn "$REPO/plugin" "$pdir/plugins/mnemosyne"
done
# the legacy pip provider must never serve tool calls alongside mnemo-pg
"$PY" "$REPO/scripts/quarantine_legacy_plugin.py" || log "WARN quarantine_legacy_plugin failed (continuing)"
# G3 quarantines legacy dirs but does NOT create bindings; step 2 owns the
# symlinks, so they must be (re)created AFTER quarantine runs, or G3 leaves
# the host serving NO provider (kim 2026-10-31 pass-1 incident).
ln -sfn "$REPO/plugin" "$HOME/.hermes/plugins/mnemosyne"
for pdir in "$HOME"/.hermes/profiles/*/; do
  [ -d "$pdir" ] || continue
  mkdir -p "$pdir/plugins"
  ln -sfn "$REPO/plugin" "$pdir/plugins/mnemosyne"
done

log "step 3: provision schemas"
SCHEMAS="$("$PY" - <<'PY'
import pathlib
# every profile on this host -> mnemosyne_<p>, plus the shared surface.
# Hidden dirs and retired (<name>.deleted.<date>) dirs are not profiles.
home = pathlib.Path.home() / ".hermes" / "profiles"
names = sorted(
    p.name for p in home.glob("[!.]*/")
    if p.is_dir() and not (".deleted." in p.name or p.name.endswith(".deleted")))
print(" ".join("mnemosyne_" + n for n in names) + " mnemosyne_shared mnemosyne_default")
PY
)"
for s in $SCHEMAS; do
  "$PY" "$REPO/scripts/provision_schema.py" "$s" || log "WARN provision $s failed"
done

log "step 4: canonical lineage fix"
for s in $SCHEMAS; do
  "$PY" "$REPO/scripts/canonical_lineage_fix.py" "$s" 2>/dev/null || log "note lineage $s reported issues (see converge next)"
done

log "step 5: core-contract convergence (idempotent)"
"$PY" "$REPO/scripts/converge_core_contract.py"

log "step 5b: re-provision (rebuild tables step 5 dropped as empty)"
for s in $SCHEMAS; do
  "$PY" "$REPO/scripts/provision_schema.py" "$s" >/dev/null 2>&1 || log "WARN re-provision $s failed"
done

log "step 5c: core-contract shape (NOT NULL/DEFAULT from core guards)"
"$PY" "$REPO/scripts/converge_core_contract_shape.py" \
  || log "WARN shape convergence reported failures"

log "step 5d: proof — connect-time heal passes the core guard"
"$PY" "$REPO/scripts/proof_device_id_heal.py" \
  || log "WARN device_id heal proof FAILED (init will hit schema mismatch)"

log "step 5e: nullable-default shape (pinned NOT-NULL-no-default class, kim 2026-10-31)"
"$PY" "$REPO/scripts/converge_nullable_default_shape.py" \
  || log "WARN nullable-default shape convergence reported failures"

log "step 5f: pinned boolean twins WITHOUT default (converge 5e skips them; core SQL needs bigint)"
"$PY" "$REPO/scripts/apply_sql.py" "$REPO/scripts/fix_pinned_boolean_no_default.sql" \
  || log "WARN pinned boolean fix reported failures"

log "step 5f2: episodic_memory summary/summary_tsv drift (INSERT trigger crash class)"
"$PY" "$REPO/scripts/apply_sql.py" "$REPO/scripts/fix_episodic_summary_tsv.sql" \
  || log "WARN episodic summary_tsv fix reported failures"

log "step 5g: timestamp-column shape -> text (btrim(timestamptz) recall crash class)"
"$PY" "$REPO/scripts/converge_ts_text_shape.py" \
  || log "WARN ts-text shape convergence reported failures"

log "step 6: live route per profile"
"$PY" "$REPO/scripts/live_route.py" || log "WARN live_route reported failures above"
log "step 6b: G1 stale-provider scan (resurrection root cause 2026-10-31)"
"$PY" "$REPO/scripts/scan_stale_providers.py" || log "G1 FAIL: stale mnemosyne provider dirs serveable - quarantine them"
log "step 6c: live-init probe per profile (real provider init -> NativeConn)"
for pdir in "$HOME"/.hermes/profiles/*/; do
  [ -d "$pdir" ] || continue
  p="$(basename "$pdir")"
  case "$p" in *.deleted.*) continue ;; esac
  out="$(MNEMO_BACKEND=postgres "$PY" "$REPO/scripts/init_probe.py" "$p" 2>/dev/null | grep '^INIT' || true)"
  echo "  $out"
  case "$out" in *": ok"*) ;; *) log "PROBE FAIL $p (init did not reach NativeConn)" ;; esac
done

if [ "$RESTART" = "1" ]; then
  log "step 7: restart surfaces (gateway picks up provider via drain-aware path)"
  if systemctl --user is-active --quiet hermes-gateway 2>/dev/null; then
    systemctl --user restart hermes-gateway
  else
    log "no user hermes-gateway unit; surfaces left as-is"
  fi
fi

log "mnemo_update: DONE"
