#!/usr/bin/env python3
"""retire_check.py — tripwire: alarm if live mnemosyne SQLite DBs exist.

Scope rules (Phase 0, 2026-10-30):
  - backups/, mnemo-pg/ and cache/ trees are excluded (archives, scratch).
  - ZERO-BYTE files are probe artifacts, not live stores (e.g. a probe that
    opened a path with URI mode=ro: SQLite creates the file then fails —
    that means SQLite is NOT active for that store; not a resurrection).
  - A file with size > 0 AND mtime inside the last LOOKBACK_S seconds is a
    LIVE sqlite write = resurrection = FAIL.
  - A file with size > 0 and old mtime = leftover archive that a retirement
    run missed = WARN (alarm on FRESH mtime still catches resurrection even
    while leftovers linger).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERMES = Path.home() / ".hermes"
LOOKBACK_S = int(os.environ.get("MNEMO_TRIPWIRE_LOOKBACK", "21600"))  # 6 h


def scan(hermes: Path = HERMES):
    now = time.time()
    fresh, leftover = [], []
    for p in hermes.rglob("*.db"):
        s = str(p)
        if "mnemosyne" not in p.name.lower():
            continue
        if "/backups/" in s or "/mnemo-pg/" in s or "/cache/" in s:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size == 0:
            continue  # probe artifact (ro-URI touch), not a live store
        if now - st.st_mtime <= LOOKBACK_S:
            fresh.append((p, st))
        else:
            leftover.append((p, st))
    return fresh, leftover


def main() -> int:
    fresh, leftover = scan()
    rc = 0
    if fresh:
        print("TRIPWIRE: live mnemosyne sqlite files RESURRECTED:")
        for p, st in fresh:
            print(f"   {p}  size={st.st_size}  mtime={time.strftime('%F %T', time.localtime(st.st_mtime))}")
        rc = 1
    if leftover:
        print(f"WARN: {len(leftover)} unarchived mnemosyne sqlite file(s) (old mtime, not live):")
        for p, st in leftover:
            print(f"   {p}  size={st.st_size}  mtime={time.strftime('%F %T', time.localtime(st.st_mtime))}")
    # env-side tripwire (2026-10-30): the legacy sqlite-era pip packages must
    # stay out of every Hermes install env -- their entry points let Hermes
    # discovery serve the pre-PG provider before any plugin can intervene.
    wipe = HERMES.parent / "workspace/mnemo-pg/repo/scripts/wipe_legacy_site_packages.py"
    if wipe.exists():
        import subprocess
        r = subprocess.run([sys.executable, "-I", str(wipe)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("TRIPWIRE: legacy mnemosyne packages RESURRECTED in env(s):")
            print((r.stdout or "").strip())
            rc = 1
    if not fresh and not leftover:
        print("tripwire clean")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
