#!/usr/bin/env python3
"""Report tables whose contract natural key is absent from an existing schema.

The native DDL creates CREATE TABLE IF NOT EXISTS (columns skipped when the
table exists), so a legacy table missing its natural key (id / event_id /
validation_id) makes the uq_* UNIQUE index fail with UndefinedColumn. This
lists the gaps so shape_pass (or a manual ALTER) can add the column.
"""
from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from mnemo_backends import psycopg_site as _ps
_ps.ensure_psycopg()
import psycopg
import provision_schema  # for dsn()
import native_ddl

def main(argv):
    schemas = argv or ["mnemosyne_mike"]
    conn = psycopg.connect(provision_schema.dsn(), autocommit=True)
    bad = 0
    for s in schemas:
        have = set()
        for t in native_ddl.TABLES:
            name = t[0]
            cols = t[1]
            kcols = [c.split()[0] for c in cols if c.endswith(" PRIMARY KEY")]
            if not kcols:
                continue
            rows = conn.execute(
                "SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0", (s, name)).fetchall()
            if not rows:
                continue  # table absent entirely; CREATE covers it
            havecols = {r[0] for r in rows}
            missing = [k for k in kcols if k not in havecols]
            if missing:
                bad += 1
                print(f"MISSING {s}.{name}: {', '.join(missing)}")
    conn.close()
    print("KEY-GAPS:", "OK" if bad == 0 else f"{bad} gaps")
    return 0 if bad == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
