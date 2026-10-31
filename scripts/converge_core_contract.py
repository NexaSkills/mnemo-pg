#!/usr/bin/env python3
"""Codify the 2026-10-30 kim fix session: converge legacy-lineage schemas
to the vendored core's runtime contract so provider init survives.

The vendored core (beam.py _init_beam_locked) runs CREATE/CREATE INDEX
statements on EVERY connect and VALIDATES existing columns against its
declarations (_add_column_if_missing). Three classes of legacy divergence
kill provider init (-> _beam None -> 'memory_unavailable' on every tool
call):

  A. Missing columns/tables (memory_events.*, memoria_facts.key, ...):
     handled by scripts/core_contract_converge.sql (guarded ADD COLUMN +
     backfill from native_ddl twin columns).
  B. Wrong DEFAULT/type on existing columns ('schema mismatch ...
     expected INTEGER DEFAULT 0, got INTEGER DEFAULT None'): handled by
     scripts/core_contract_converge2.sql (catalog-guarded SET DEFAULT /
     TYPE where the core's own _add_column_if_missing declarations
     demand it).
  C. Empty legacy tables that CREATE IF NOT EXISTS can never converge:
     handled by scripts/drop_empty_legacy_tables.sql (drop EMPTY only,
     then re-run provision_schema.py to rebuild from native_ddl).

Plus the public-shadow door (scripts/drop_empty_public_shadow.sql):
unqualified DDL in the core resolves against `public` first if stale
probe tables live there.

This runner applies them in order for a whole database (idempotent,
guarded; safe to run any number of times). Data-bearing divergent
tables are never dropped; they go through the ALTER paths.

Usage: converge_core_contract.py [dsn]   (dsn default: the local
lcm-pg cluster, same resolution as provision_schema.py)
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SQL_FILES = (
    "core_contract_converge.sql",     # A: columns + backfills
    "core_contract_converge2.sql",    # B: defaults/types the core validates
    "drop_empty_legacy_tables.sql",   # C1: empty legacy tables only
    "drop_empty_public_shadow.sql",   # C2: public must shadow zero app tables
)


def _dsn() -> str:
    """Same resolution order as provision_schema.dsn() (env, HERMES_HOME
    .env, then the local lcm-pg socket dir) — single source of truth."""
    sys.path.insert(0, str(HERE))
    import provision_schema
    return provision_schema.dsn()


def main() -> int:
    dsn = sys.argv[1] if len(sys.argv) > 1 else _dsn()
    for name in SQL_FILES:
        path = HERE / name
        print(f"== {name}")
        r = subprocess.run(
            ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(path)],
            stdout=sys.stderr, stderr=sys.stderr)
        if r.returncode != 0:
            print(f"FAILED {name} (rc={r.returncode})", file=sys.stderr)
            return r.returncode
    print("core contract convergence complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
