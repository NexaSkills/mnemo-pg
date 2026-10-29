#!/usr/bin/env python3
"""retire_check.py — tripwire: alarm if live mnemosyne SQLite DBs exist."""
import pathlib, sys

HERMES = pathlib.Path.home() / ".hermes"

def main():
    live = []
    for p in HERMES.rglob("*.db"):
        s = str(p)
        if "mnemosyne" not in p.name.lower():
            continue
        if "/backups/" in s or "/mnemo-pg/" in s:
            continue
        live.append(p)
    if live:
        print("TRIPWIRE: live mnemosyne sqlite files RESURRECTED:")
        for p in live:
            print("  ", p)
        return 1
    print("tripwire clean")
    return 0

if __name__ == "__main__":
    sys.exit(main())
