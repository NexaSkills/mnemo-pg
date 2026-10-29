"""julianday()/strftime() parity functions for the mnemo-pg schemas.

The vendored core uses SQLite's julianday() for temporal validity predicates
(valid_until IS NULL OR julianday(valid_until) > julianday(?)). The core
writes valid_until as an ISO-8601 TEXT string (datetime.fromisoformat in
beam.py/memory.py), so julianday() has to accept both ISO text and epoch
REAL/DOUBLE columns and return SQLite's Julian-day epoch seconds scale.

Postgres has no julianday(). We create one per schema, IMMUTABLE so the
planner can fold it, registered for the concrete types the core stores:
  - float8   : epoch seconds -> /86400 + 2440587.5 (Julian day of 1970 epoch)
  - timestamp: same via extract(epoch from ...)
  - text     : try ISO parse; fall back to epoch numeric parse; NULL if neither
The text form uses a plpgsql function with an exception handler so a
malformed string returns NULL like SQLite's julianday() would.
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import os
import re
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# single source of truth: the live connect path installs the SAME definitions
from mnemo_backends.date_fns import FUNCTIONS
from mnemo_backends import date_fns_extra


def dsn() -> str:
    d = os.environ.get("MNEMO_PG_DSN")
    if d:
        return d
    run = pathlib.Path.home() / "lcm-pg" / "run"
    return f"host={run} port=54329 dbname=lcm user=lcm01"


def apply(dsn: str, schema: str) -> int:
    import psycopg
    n = 0
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path = "{schema}", public')
            for fn in list(FUNCTIONS) + date_fns_extra.sql_for(schema):
                try:
                    cur.execute(fn)
                    n += 1
                except psycopg.Error:
                    conn.rollback()
    return n


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("Usage: python3 scripts/date_fns_parity.py <schema>  (or --all for mnemosyne_*)")
        return 1
    target = sys.argv[1]
    if target == "--all":
        schemas = ["mnemosyne_default", "mnemosyne_mike", "mnemosyne_brandbuddy",
                   "mnemosyne_debbie", "mnemosyne_buttercup", "mnemosyne_shared"]
    else:
        schemas = [target]
    for s in schemas:
        n = apply(dsn(), s)
        print(f"{s}: {n}/{len(FUNCTIONS)} functions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
