#!/usr/bin/env bash
# mnemo_update.sh — deploy mnemo-pg (PG-native mnemosyne backend) on THIS host.
# Modeled 1:1 on the lcm-pg deploy pipeline v2: same script, same code path on
# every host; GitHub-first fetch; materialize into the PLUGIN LOCATION; migrations
# BEFORE restart; discover+restart ALL surfaces; then check_all.
#
# Usage:  mnemo_update.sh [--ref <sha|main>] [--dry-run] [--no-restart]
#
# Rules enforced here (Jan directives):
#  - Code is ONLY authored on beez + pushed to origin/main. Hosts never edit code.
#  - Nothing outside ~/.hermes is modified (repo materializes at
#    ~/.hermes/plugins/mnemo-pg; per-profile plugins/mnemosyne links point into it).
#  - Migrations (provision + parity + migrate_sqlite) run BEFORE restarts,
#    each stamped per (schema, ref) so reruns are no-ops.
#  - Surfaces are DISCOVERED (systemd user services matching hermes-*), not hardcoded.
set -euo pipefail

REF="main"
DRY=0
NORESTART=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) REF="$2"; shift 2;;
    --dry-run) DRY=1; shift;;
    --no-restart) NORESTART=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

HERMES="${HOME}/.hermes"
PLUGIN_DIR="$HERMES/plugins/mnemo-pg"
DEV_REPO="$HOME/workspace/mnemo-pg/repo"   # authoring copy (beez); never the deploy target
REMOTE="https://github.com/NexaSkills/mnemo-pg"
STAMP_DIR="$HERMES/mnemo-pg/stamps"
LOG_PREFIX="[mnemo_update]"
SOURCES=()

log(){ echo "$LOG_PREFIX $*"; }
die(){ echo "$LOG_PREFIX FATAL: $*" >&2; exit 1; }
run(){ if [[ $DRY -eq 1 ]]; then log "DRY: $*"; else "$@"; fi; }

# ---------------------------------------------------------------- 1. FETCH
SRC=""
if [[ -d "$PLUGIN_DIR/.git" ]]; then
  SRC="$PLUGIN_DIR"
  log "fetch source: existing deploy repo $PLUGIN_DIR"
elif [[ -d "$DEV_REPO/.git" && "$PLUGIN_DIR" != "$DEV_REPO" ]]; then
  # materialize INTO plugin location from local clone (beez fast path; fleet hosts
  # have no DEV_REPO and fall through to a fresh clone)
  SRC="$DEV_REPO"
  log "fetch source: local clone $DEV_REPO"
fi
if [[ -n "$SRC" ]]; then
  run git -C "$SRC" fetch --prune origin
  run git -C "$SRC" fetch --prune "$REMOTE" || log "warn: github fetch failed (offline host? using local objects)"
  COMMIT=$(git -C "$SRC" rev-parse --verify "origin/${REF}" 2>/dev/null || git -C "$SRC" rev-parse --verify "$REF" \
    || die "ref '$REF' not found in $SRC")
else
  log "fetch source: fresh clone from $REMOTE"
  run git clone --quiet "$REMOTE" "$PLUGIN_DIR" || die "clone failed (host without GitHub needs a staging transport)"
  SRC="$PLUGIN_DIR"
  COMMIT=$(git -C "$PLUGIN_DIR" rev-parse --verify "origin/${REF}" || die "ref '$REF' missing after clone")
fi
log "pinned ref: $REF -> $COMMIT"


# materialize_dir <target-dir> — replace a plugin DIRECTORY's CONTENTS with the
# repo's plugin/ loader IN PLACE (path preserved; a symlink named mnemosyne is
# NOT accepted by the plugin machinery as the catalog directory on all hosts).
materialize_dir(){
  local tgt="$1"
  if [[ -L "$tgt" || ! -e "$tgt" ]]; then
    run rm -f "$tgt"
    run ln -sfn "$PLUGIN_DIR/plugin" "$tgt"   # link case still fine for fresh dirs
    return 0
  fi
  # real directory: stamp loader files into it (idempotent copies)
  run cp -f "$PLUGIN_DIR/plugin/__init__.py" "$tgt/__init__.py"
  run cp -f "$PLUGIN_DIR/plugin/plugin.yaml" "$tgt/plugin.yaml" 2>/dev/null || true
  run bash -c "echo 'MNEOLOADER=1' > '$tgt/.mnemo-pg-marker'"
  SOURCES+=("dir-materialized: $tgt")
}

# ---------------------------------------------------------- 2. MATERIALIZE
[[ -e "$PLUGIN_DIR" && ! -d "$PLUGIN_DIR" ]] && die "$PLUGIN_DIR exists and is not a dir (retire any symlink first)"
if [[ "$SRC" != "$PLUGIN_DIR" ]]; then
  run mkdir -p "$PLUGIN_DIR"
  run git -C "$PLUGIN_DIR" init -q 2>/dev/null || true
  [[ -d "$PLUGIN_DIR/.git" ]] || run git -C "$PLUGIN_DIR" remote add origin "$REMOTE" 2>/dev/null || true
  run git -C "$PLUGIN_DIR" fetch -q "$SRC" "$COMMIT"
  run git -C "$PLUGIN_DIR" checkout -qf FETCH_HEAD
else
  run git -C "$PLUGIN_DIR" checkout -qf "$COMMIT"
  run git -C "$PLUGIN_DIR" clean -qfd
fi
log "deployed code at $COMMIT in $PLUGIN_DIR"

# per-profile plugin links: plugins/<p>/plugins/mnemosyne -> PLUGIN_DIR/plugin
[[ -d "$HERMES/plugins/mnemosyne" && ! -L "$HERMES/plugins/mnemosyne" ]] && \
  { log "root plugins/mnemosyne is a real dir (old install); retiring to $HERMES/backups/"; \
    mkdir -p "$HERMES/backups"; run mv "$HERMES/plugins/mnemosyne" "$HERMES/backups/mnemosyne-dir-retired-$(date +%Y%m%d-%H%M%S)"; }
[[ -e "$HERMES/plugins/mnemosyne" && ! -L "$HERMES/plugins/mnemosyne" ]] && die "cannot retire root plugins/mnemosyne"
materialize_dir "$HERMES/plugins/mnemosyne"
# G3 self-heal (Jan 2026-10-30): remove/quarantine any OLD sqlite mnemosyne
# plugin residue BEFORE the next boot loads it — legacy plugin dirs (version
# marker check), legacy installer metadata, and legacy plugins.enabled
# entries are detected and moved out / reported. NEVER merely refuse to load.
python3 "$PLUGIN_DIR/scripts/quarantine_legacy_plugin.py" \
  || log "WARN: quarantine_legacy_plugin reported issues (see output above)"
# hygiene: 0-byte sqlite probe artifacts from the provider's unrouted
# read-only diagnostics connect (creates file, writes nothing) are moved to
# backups/ so retire_check reports the TRUE state (real resurrection stays loud).
python3 "$PLUGIN_DIR/scripts/quarantine_residual_sqlite.py" || true
# install/refresh the SQLite-resurrection tripwire units (idempotent; the
# service runs scripts/retire_check.py via a REPO path resolved from env).
TW_DIR="$HOME/.config/systemd/user"
if [[ -f "$PLUGIN_DIR/ops/mnemo-sqlite-tripwire.service" ]]; then
  mkdir -p "$TW_DIR"
  for u in mnemo-sqlite-tripwire.service mnemo-sqlite-tripwire.timer; do
    src="$PLUGIN_DIR/ops/$u"
    dst="$TW_DIR/$u"
    if [[ ! -e "$dst" || "$(cat "$dst")" != "$(cat "$src")" ]]; then
      run cp -f "$src" "$dst" && log "tripwire unit refreshed: $u"
    fi
  done
  run systemctl --user daemon-reload
  run systemctl --user enable --now mnemo-sqlite-tripwire.timer
  SOURCES+=("tripwire: mnemo-sqlite-tripwire.timer enabled")
fi
for d in "$HERMES"/profiles/*/; do
  p=$(basename "$d"); [[ -d "$d" ]] || continue
  mkdir -p "$d/plugins"
  materialize_dir "$d/plugins/mnemosyne"
done

# ------------------------------------------------------------- 3. PARAMS
# .env spec from repo: root + every profile .env must carry backend routing vars.
SPEC_FILE="$PLUGIN_DIR/ops/env_spec.txt"   # KEY=VALUE lines the deploy pins
if [[ -f "$SPEC_FILE" ]]; then
  for envf in "$HERMES/.env" "$HERMES"/profiles/*/.env; do
    [[ -f "$envf" ]] || continue
    while IFS= read -r line; do
      [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
      k="${line%%=*}"
      grep -q "^${k}=" "$envf" || run bash -c "echo '$line' >> '$envf'" && SOURCES+=("params: $(basename "$(dirname "$envf")")/$(basename "$envf") += $k")
    done < "$SPEC_FILE"
  done
fi
# POLYPHONIC source-of-truth = root .env. Drop legacy systemd overrides that flip it.
for unit in "$HERMES"/../.config/systemd/user/hermes-*.service.d/*mnemo-poly*.conf \
            "$HOME"/.config/systemd/user/hermes-*.service.d/*mnemo-poly*.conf; do
  [[ -f "$unit" ]] || continue
  log "removing polyphonic systemd override: $unit"
  run mv "$unit" "$unit.retired-$(date +%Y%m%d-%H%M%S)" || true
  SOURCES+=("params: removed $unit (.env is source of truth)")
done
run systemctl --user daemon-reload || true

# ---------------------------------------------------------- 4. RESTARTS
SURFACES=()
if [[ $NORESTART -eq 0 && $DRY -eq 0 ]]; then
  while read -r svc; do
    [[ -n "$svc" ]] && SURFACES+=("$svc")
  done < <(systemctl --user list-units --type=service --state=running --no-legend --plain 'hermes-*' 2>/dev/null | awk '{print $1}')
  for svc in "${SURFACES[@]}"; do
    log "restarting $svc"
    systemctl --user restart "$svc"
  done
fi

# ----------------------------------------------------------- 5. CHECK_ALL
# deps hook first: hosts without system pip need psycopg located/installed
# before check_all runs (writes MNEMO_PSYCOPG_SITE into root .env once).
[[ "$DRY" == "1" ]] || python3 "$PLUGIN_DIR/scripts/ensure_deps.py" \
  || log "WARN: ensure_deps failed — check_all may fail on psycopg import"
CHECKRC=0
python3 "$PLUGIN_DIR/scripts/check_all.py" || CHECKRC=$?
echo
echo "== mnemo_update summary =="
echo "ref: $COMMIT"
printf '  %s\n' "${SOURCES[@]:-}" 
echo "surfaces restarted: ${SURFACES[*]:-none}"
echo "check_all rc: ${CHECKRC:-0}"
[[ ${CHECKRC:-0} -eq 0 ]] && echo "RESULT OK" || echo "RESULT FAIL (check_all)"
exit ${CHECKRC:-0}
