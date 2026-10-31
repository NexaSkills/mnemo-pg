#!/usr/bin/env python3
"""scan_hermes_sqlite_stores.py — inventory of non-memory SQLite stores owned
by the Hermes host (Jan directive 2026-10-31: NO sqlite mechanisms allowed —
anything found must be converted to native PG or proven ephemeral).

This is an INVENTORY, not a tripwire: it lists every live (size>0) .db file
under HERMES_HOME that is NOT a mnemosyne/lcm memory store (those are covered
by retire_check.py and must be zero), plus sqlite3.connect call sites in the
Hermes source, so conversion work can be planned. Exit 0 always (inventory);
exit 1 only on scan failure.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
MEMORY_TOKENS = ("mnemosyne", "lcm")          # owned by retire_check.py
EXCL_DIRS = ("/backups/", "/cache/", "/mnemo-pg/", "/installs/",
             "/hermes-agent/", "/.git/", "/sessions/")


def scan_stores() -> list[tuple[Path, int, float]]:
    import time
    now = time.time()
    rows = []
    for p in HERMES.rglob("*.db"):
        s = str(p)
        if any(tok in p.name.lower() for tok in MEMORY_TOKENS):
            continue
        if any(x in s for x in EXCL_DIRS):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size == 0:
            continue
        rows.append((p, st.st_size, now - st.st_mtime))
    return sorted(rows, key=lambda r: r[2])


def scan_connect_sites(repo: Path) -> list[str]:
    if not (repo / "gateway").is_dir():
        return [f"(source tree not found at {repo})"]
    out = subprocess.run(
        ["grep", "-rn", "--include=*.py", "sqlite3.connect",
         str(repo / "gateway"), str(repo / "tools"), str(repo / "hermes_cli"),
         str(repo / "agent"), str(repo / "run_agent.py")],
        capture_output=True, text=True)
    lines = [l for l in out.stdout.splitlines() if "/tests" not in l]
    return lines


def main() -> int:
    print(f"HERMES_HOME={HERMES}")
    stores = scan_stores()
    print(f"\nlive non-memory .db stores: {len(stores)}")
    for p, size, age in stores:
        age_h = age / 3600
        fresh = "FRESH" if age < 3600 else "idle"
        print(f"  [{fresh:>5}] {p}  {size}B  last-write {age_h:.1f}h ago")
    repo = Path.home() / ".hermes" / "hermes-agent"
    sites = scan_connect_sites(repo)
    print(f"\nsqlite3.connect call sites in Hermes source: {len(sites)}")
    # group by file for brevity
    from collections import Counter
    files = Counter(s.split(":", 1)[0].replace(str(repo) + "/", "") for s in sites)
    for f, n in files.most_common():
        print(f"  {n:4d}  {f}")
    print("\n(NOTE: memory stores — mnemosyne/lcm — are fail-loud PG-only via"
          " mnemo-pg; this inventory covers the remaining Hermes-internal stores"
          " for PG conversion planning.)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"scan failed: {exc}", file=sys.stderr)
        sys.exit(1)
