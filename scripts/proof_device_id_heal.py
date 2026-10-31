#!/usr/bin/env python3
"""One-shot proof that the connect-time core-contract self-heal works:
reproduces the device_id class (2026-10-31) on a scratch schema, then
connects via the factory and runs the core's own validator guard.

PASS  = device_id decl carries NOT NULL+DEFAULT, connect heals the
        nullable column, and _add_column_if_missing accepts it.
Usage: proof_device_id_heal.py
Exit 0 on pass, 1 on fail. Cleans up its scratch schema.
"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/workspace/mnemo-pg/repo"))
sys.path.insert(0, os.path.expanduser("~/workspace/mnemo-pg/repo/vendor"))

DSN = ("host=%s port=54329 dbname=lcm user=lcm01"
       % os.path.expanduser("~/lcm-pg/run"))
SCH = "mnemosyne_probe_heal"

import psycopg  # noqa: E402

ok = True
with psycopg.connect(DSN, autocommit=True) as r:
    r.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCH)
    r.execute("CREATE SCHEMA %s" % SCH)
    r.execute("CREATE TABLE %s.memory_events "
              "(event_id text PRIMARY KEY, payload text, "
              "device_id text DEFAULT '')" % SCH)

from mnemo_backends import core_declared as cd  # noqa: E402
decl = cd.declared_columns()["memory_events"].get("device_id", "")
print("decl:", repr(decl))
if "NOT NULL" not in decl.upper():
    print("FAIL: decl lost NOT NULL")
    ok = False

os.environ["MNEMO_PG_SCHEMA"] = SCH
os.environ["MNEMO_PG_DSN"] = DSN
from mnemo_backends.factory import connect as pg_connect  # noqa: E402
from mnemosyne.core.beam import _add_column_if_missing  # noqa: E402

conn = pg_connect()
row = conn.execute(
    "select attnotnull::int from pg_attribute a "
    "join pg_class k on k.oid = a.attrelid "
    "join pg_namespace n on n.oid = k.relnamespace "
    "and n.nspname = current_schema() "
    "where k.relname = 'memory_events' and a.attname = 'device_id'"
).fetchone()
# adapter rows proxy sqlite rows: index/tuple access only (no .values here)
nn = bool(tuple(row)[0]) if row else False
print("notnull after connect-heal:", nn)
if not nn:
    print("FAIL: connect-time heal did not SET NOT NULL")
    ok = False
try:
    _add_column_if_missing(conn, "memory_events", "device_id",
                           "TEXT NOT NULL DEFAULT ''")
    print("core guard: PASS")
except Exception as e:
    print("core guard: RAISE", str(e)[:120])
    ok = False

with psycopg.connect(DSN, autocommit=True) as r:
    r.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCH)

print("PROOF:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
