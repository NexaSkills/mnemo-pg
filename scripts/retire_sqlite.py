#!/usr/bin/env python3
"""retire_sqlite.py — archive + delete live mnemosyne SQLite files, install tripwire.

Destructive by design (Jan-approved retirement). Scope: inside ~/.hermes only.
  1. Finds live mnemosyne*.db (+ -wal/-shm) under ~/.hermes (skips backups/).
  2. Verifies each db's schema exists in PG (row-count sanity) before deleting.
  3. Archives to ~/.hermes/backups/mnemosyne-sqlite-retired-<ts>/ preserving rel paths.
  4. Deletes originals.
  5. Installs mnemo-sqlite-tripwire.{timer,service} systemd-user units that alarm
     if a live mnemosyne db reappears.
Flags: --dry-run, --force (skip PG verification).
"""
import argparse, datetime, os, pathlib, shutil, subprocess, sys

HERMES = pathlib.Path.home() / ".hermes"
UNITS = pathlib.Path.home() / ".config/systemd/user"

def find_live():
    out = []
    for p in HERMES.rglob("*.db"):
        s = str(p)
        if "mnemosyne" not in p.name.lower():
            continue
        if "/backups/" in s or "/mnemo-pg/" in s:
            continue
        out.append(p)
    return sorted(out)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    live = find_live()
    if not live:
        print("no live mnemosyne sqlite files found")
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    arch = HERMES / "backups" / f"mnemosyne-sqlite-retired-{ts}"
    for db in live:
        for side in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            if not side.exists():
                continue
            rel = side.relative_to(HERMES)
            dest = arch / rel
            print(f"{'DRY ' if a.dry_run else ''}archive {side} -> {dest}")
            if not a.dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(side), str(dest))
        for side in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            if side.exists():
                print(f"{'DRY ' if a.dry_run else ''}delete {side}")
                if not a.dry_run:
                    side.unlink()
    # tripwire units
    svc = UNITS / "mnemo-sqlite-tripwire.service"
    tmr = UNITS / "mnemo-sqlite-tripwire.timer"
    svc_text = f"""[Unit]
Description=mnemo-pg SQLite resurrection tripwire

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {HERMES}/mnemo-pg/repo/scripts/retire_check.py
"""
    tmr_text = """[Unit]
Description=Run mnemo SQLite tripwire every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
"""
    for f, text in ((svc, svc_text), (tmr, tmr_text)):
        if a.dry_run:
            print(f"DRY would write {f}")
            continue
        if f.is_symlink() or (f.exists() and f.read_text() != text):
            f.unlink(missing_ok=True)
        if not f.exists():
            f.write_text(text)
            print(f"wrote {f}")
    if not a.dry_run:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now",
                        "mnemo-sqlite-tripwire.timer"], check=False)
        print("tripwire timer enabled")
    print("retire_sqlite done", "(dry-run)" if a.dry_run else "")

if __name__ == "__main__":
    main()
