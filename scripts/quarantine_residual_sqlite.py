#!/usr/bin/env python3
"""quarantine_residual_sqlite.py — quarantine the SQLite files an UNROUTED
read-only code path touches on every provider boot (Phase 0, 2026-10-30).

Mechanism (verified on beez): vendor/hermes_memory_provider/__init__.py
diagnostics open `sqlite3.connect(str(beam.db_path))` via a RAW stdlib import
(a late `import sqlite3` inside the function body, invisible to the binding
sweep), under the detached surfaces' profile-pinned HERMES_HOME with no
MNEMO_PG_DSN in env. factory.connect therefore cannot route it and SQLite
honestly creates the 0-byte DB + WAL sidecars at
<profile>/mnemosyne/data/mnemosyne.db{,-wal,-shm}. Content NEVER lands there
(read-only connect, and the true store is the PG schema), but the fresh files
look like SQLite resurrection to any census.

YAGNI stance: this is NOT a fix to the diagnostics path itself (the vendored
core is upstream code); it is the deploy's hygiene step: any 0-byte
mnemosyne*.db (+ -wal/-shm) outside backups/ is a probe artifact and is moved
into ~/.hermes/backups/mnemosyne-probe-artifacts-<stamp>/. Non-empty files are
LEFT for retire_check.py to alarm on (real resurrection stays loud).

Idempotent. Flags: --dry-run.
"""
from __future__ import annotations
import argparse
import datetime
import pathlib
import shutil
import sys

HERMES = pathlib.Path.home() / ".hermes"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    arch = HERMES / "backups" / f"mnemosyne-probe-artifacts-{ts}"
    moved = 0
    for p in sorted(HERMES.rglob("*.db")):
        s = str(p)
        if "mnemosyne" not in p.name.lower():
            continue
        if "/backups/" in s or "/mnemo-pg/" in s:
            continue
        if p.exists() and p.stat().st_size > 0:
            # a real DB with content is retire_check's territory (loud FAIL
            # on fresh mtime, WARN on stale) — never silently moved here.
            continue
        live = [f for f in (p, p.with_name(p.name + "-wal"),
                            p.with_name(p.name + "-shm")) if f.exists()]
        for f in live:
            dest = arch / f.relative_to(HERMES)
            print(f"{'DRY ' if a.dry_run else ''}quarantine {f} -> {dest}")
            if not a.dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(dest))
            moved += 1
    print(f"quarantine_residual_sqlite: {moved} probe artifact(s)"
          f"{' (dry-run)' if a.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
