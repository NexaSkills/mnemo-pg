#!/usr/bin/env python3
"""Converge every mnemosyne_* schema to the core's validator shape.

The deploy twin of the runtime connect-time heal (mnemo_backends/
core_contract.py): same generator (core_declared.declared_columns parses
the vendored core's own guards), applied to every schema in the cluster
without needing a NativeConn connect. Idempotent, best-effort per column
(a failing fix is counted, never fatal), and schema-local (never public).

Fixes applied per existing column:
  * type drift          -> ALTER TYPE ... USING (safe-cast pairs only)
  * DEFAULT declared, none in PG -> SET DEFAULT
  * NOT NULL declared, PG nullable -> backfill DEFAULT (if declared), then
    SET NOT NULL (beam.py _column_matches compares notnull exactly)
  * NOT NULL forced, core doesn't require -> DROP NOT NULL

Usage: converge_core_contract_shape.py [dsn]
"""
import re
import sys
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from mnemo_backends.core_declared import declared_columns  # noqa: E402
from mnemo_backends.core_contract import _want_pg, _CAST_OK  # noqa: E402


def _dsn() -> str:
    sys.path.insert(0, str(HERE))
    import provision_schema
    return provision_schema.dsn()


def main() -> int:
    dsn = sys.argv[1] if len(sys.argv) > 1 else _dsn()
    decls = declared_columns()
    if not decls:
        print("no core declarations parsed (vendor core missing?)", file=sys.stderr)
        return 2
    total = 0
    with psycopg.connect(dsn, autocommit=True) as root:
        schemas = [r[0] for r in root.execute(
            "select nspname from pg_namespace where nspname like 'mnemosyne_%' "
            "and nspname not like '%deleted%' order by 1")]
    for sch in schemas:
        fixes = skipped = 0
        with psycopg.connect(dsn + ("&options=-c%%20search_path%%3D%s" % sch
                                    if "?" in dsn else
                                    " options=-c\\ search_path=%s" % sch),
                             autocommit=True) as raw:
            # set search_path via SET (portable; no URL quoting games)
            raw.execute("SET search_path = %s" % sch.replace('"', '""'))
            with raw.cursor() as c:
                c.execute(
                    "select t.relname, a.attname, "
                    "format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                    "pg_get_expr(d.adbin, d.adrelid) "
                    "from pg_catalog.pg_class t "
                    "join pg_catalog.pg_namespace n on n.oid = t.relnamespace "
                    "join pg_catalog.pg_attribute a on a.attrelid = t.oid "
                    "  and a.attnum > 0 and not a.attisdropped "
                    "left join pg_catalog.pg_attrdef d "
                    "  on d.adrelid = a.attrelid and d.adnum = a.attnum "
                    "where n.nspname = current_schema() and t.relkind = 'r'")
                present = {(t, col): (ty.lower(), nn, dflt)
                           for t, col, ty, nn, dflt in c.fetchall()}
            for table, cols in decls.items():
                for col, decl in cols.items():
                    have = present.get((table, col))
                    if have is None:
                        continue
                    htype, hnn, hdflt = have
                    want = _want_pg(col, decl)
                    ok = (htype == want
                          or (htype.startswith("timestamp")
                              and want.startswith("timestamp"))
                          or ({htype.split()[0], want.split()[0]}
                              == {"text", "timestamp"}))
                    if not ok:
                        if (htype, want) not in _CAST_OK:
                            skipped += 1
                            continue
                        try:
                            raw.execute(
                                'ALTER TABLE "%s"."%s" ALTER COLUMN "%s" '
                                "TYPE %s USING (%s::%s)"
                                % (sch, table, col, want, col, want))
                            fixes += 1
                        except Exception:
                            skipped += 1
                            continue
                    dm = re.search(r"\bDEFAULT\s+(.+)$", decl or "", re.I)
                    want_dflt = None
                    if dm:
                        d = dm.group(1).strip()
                        if d.upper() != "NULL" and (
                                re.fullmatch(r"-?\d+(\.\d+)?", d)
                                or re.fullmatch(r"'(?:[^']|'')*'", d)):
                            want_dflt = d
                    if want_dflt is not None and hdflt is None:
                        try:
                            raw.execute(
                                'ALTER TABLE "%s"."%s" ALTER COLUMN "%s" '
                                "SET DEFAULT %s" % (sch, table, col, want_dflt))
                            fixes += 1
                        except Exception:
                            skipped += 1
                    if re.search(r"\bNOT NULL\b", decl or "", re.I):
                        if not hnn:
                            try:
                                if want_dflt is not None:
                                    raw.execute(
                                        'UPDATE "%s"."%s" SET "%s" = %s '
                                        'WHERE "%s" IS NULL'
                                        % (sch, table, col, want_dflt, col))
                                raw.execute(
                                    'ALTER TABLE "%s"."%s" ALTER COLUMN "%s" '
                                    "SET NOT NULL" % (sch, table, col))
                                fixes += 1
                            except Exception:
                                skipped += 1
                    elif hnn:
                        try:
                            raw.execute(
                                'ALTER TABLE "%s"."%s" ALTER COLUMN "%s" '
                                "DROP NOT NULL" % (sch, table, col))
                            fixes += 1
                        except Exception:
                            skipped += 1
        print("%-28s fixes=%d skipped=%d" % (sch, fixes, skipped))
        total += fixes
    print("core-contract shape convergence complete (total fixes=%d)" % total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
