#!/usr/bin/env bash
# retire_sqlite.sh — complete retirement of the legacy SQLite mnemosyne store
# on THIS host. Runs AFTER deploy + migration + parity checks have passed.
#
# Jan-locked policy (2026-10-29):
#   1. Archive live mnemosyne.db (+ -wal/-shm) under ~/.hermes/backups/
#      mnemosyne-sqlite-retired-<ts>/  (mirrors the lcm-db retirement pattern).
#   2. Delete the live copies.
#   3. Install a systemd-user tripwire timer that ALARMS if a SQLite
#      mnemosyne.db is resurrected anywhere under ~/.hermes.
# Everything lives inside ~/.hermes.
set -euo pipefail

HERMES="$HOME/.hermes"
TS=$(date +%Y%m%d-%H%M%S)
ARCHIVE="$HERMES/backups/mnemosyne-sqlite-retired-$TS"
UNIT_DIR="$HOME/.config/systemd/user"
DRY=${DRY:-0}

log(){ echo "[retire_sqlite] $*"; }
run(){ if [[ $DRY -eq 1 ]]; then log "DRY: $*"; else "$@"; fi; }

found=0
mapfile -t dbs < <(find "$HERMES" -name 'mnemosyne.db' \
  -not -path "$HERMES/backups/*" 2>/dev/null)
if [[ ${#dbs[@]} -eq 0 ]]; then
  log "no live mnemosyne.db under $HERMES — already retired"
else
  log "found ${#dbs[@]} live db(s):"
  printf '  %s\n' "${dbs[@]}"
  # safety: refuse if any profile's PG schema still shows migration never ran
  run mkdir -p "$ARCHIVE"
  for db in "${dbs[@]}"; do
    # relpath under ~/.hermes preserves structure in the archive
    rel=${db#"$HERMES"/}
    run mkdir -p "$ARCHIVE/$(dirname "$rel")"
    run mv "$db" "$ARCHIVE/$rel"
    for ext in -wal -shm; do
      if [[ -e "$db$ext" ]]; then
        run mv "$db$ext" "$ARCHIVE/$rel$ext"
        log "archived $rel$ext"
      fi
    done
    log "archived $rel"
    found=1
  done
  # delete step: archives live on; remove any *other* copies not archived
  # (empty -shm/-wal strays)
  find "$HERMES" -name 'mnemosyne.db-*' -not -path "$HERMES/backups/*" \
    -delete 2>/dev/null || true
fi

# tripwire: systemd-user timer + service that alarms on resurrection
SCRIPT_DIR="$HERMES/ops"
run mkdir -p "$SCRIPT_DIR"
cat > "$SCRIPT_DIR/mnemo_sqlite_tripwire.sh" <<'EOF'
#!/usr/bin/env bash
# Alarm if a SQLite mnemosyne.db exists outside the retirement archive.
HERMES="$HOME/.hermes"
hits=$(find "$HERMES" -name 'mnemosyne.db' -not -path "$HERMES/backups/*" 2>/dev/null)
if [[ -n "$hits" ]]; then
  logger -t mnemo-sqlite-tripwire -p user.crit \
    "RESURRECTED SQLite mnemosyne.db found (SQLite is retired — investigate backend fallback): $hits"
  echo "TRIPWIRE: resurrected SQLite dbs:"; echo "$hits"
  exit 1
fi
echo "tripwire ok: no live SQLite mnemosyne.db"
EOF
chmod 755 "$SCRIPT_DIR/mnemo_sqlite_tripwire.sh"

if [[ $DRY -eq 0 ]]; then
  cat > "$UNIT_DIR/mnemo-sqlite-tripwire.service" <<EOF
[Unit]
Description=mnemo-pg SQLite retirement tripwire
[Service]
Type=oneshot
ExecStart=$SCRIPT_DIR/mnemo_sqlite_tripwire.sh
EOF
  cat > "$UNIT_DIR/mnemo-sqlite-tripwire.timer" <<EOF
[Unit]
Description=mnemo-pg SQLite retirement tripwire (hourly)
[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
[Install]
WantedBy=timers.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now mnemo-sqlite-tripwire.timer
  log "tripwire timer installed + enabled"
fi

log "done. archive: $ARCHIVE"
[[ $found -eq 1 ]] && log "NOTE: verify PG recall per profile before trusting live traffic"
exit 0
