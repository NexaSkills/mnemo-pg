#!/usr/bin/env python3
"""converge_core_declared — guarantee every column the vendored core DECLARES.

Why this exists (2026-10-31 manny/tessa/kim root-cause):
The core's init path runs CREATE TABLE IF NOT EXISTS + guarded
_add_column_if_missing + CREATE INDEX IF NOT EXISTS against EVERY module's
schema (beam, sync, annotations, canonical, ...). scripts/native_ddl.py only
provisions BEAM's tables — other modules' tables get created by the core at
runtime, but if a divergent lineage created them first (or an index from
module init references columns the provisioner never declared), init dies with
UndefinedColumn / schema mismatch and _beam=None: the silent memory outage
class (canonical tools -> AttributeError).

Method — DRY, no hand-maintained table list:
  1. Scan the vendored core (vendor/mnemosyne/**.py) for EVERY
     'CREATE TABLE IF NOT EXISTS <t> (<body>)' literal.
  2. For each target schema, ensure the table exists:
       - absent            -> emit the core's CREATE (native adapter rewrites
                              AUTOINCREMENT/CURRENT_TIMESTAMP at runtime).
       - present, diverged -> ADD COLUMN IF NOT EXISTS for every column in the
                              core body (nullable adds; existing columns are
                              NEVER altered here — type convergence belongs to
                              the validator-aware converge2 pass).
  3. Guard-add passes: the same scan covers _add_column_if_missing literals.

Idempotent and data-safe: adds only; no drops, no type changes (those are the
existing converge scripts' job). Run BEFORE live_route; the core's own init
then completes instead of half-failing.

Usage: converge_core_declared.py [schema ...]   (default: every mnemosyne_*)
"""

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# CREATE TABLE IF NOT EXISTS name ( cols )  — literal bodies only (no f-strings:
# verified 2026-10-31: all module CREATEs are plain literals).
# NOTE: never name a group `t` — PL/pgSQL's `t text` convention collides.
_CREATE_RE = re.compile(
    r'CREATE TABLE IF NOT EXISTS\s+"?(\w+)"?\s*\((.*?)\)\s*"""', re.S | re.I)
# fallback: python triple-quoted cursor.execute("""  CREATE TABLE ... """)
_COL_RE = re.compile(
    r'^\s*"?([a-z_][a-z0-9_]*)"?\s+[A-Za-z]', re.M)


def core_tables():
    """dict table -> list(columns) across the whole vendored core."""
    tables = {}
    for py in sorted((REPO / "vendor" / "mnemosyne").rglob("*.py")):
        src = py.read_text(errors="replace")
        for m in _CREATE_RE.finditer(src):
            tbl, body = m.group(1), m.group(2)
            cols = []
            depth = 0
            for line in body.splitlines():
                line = line.strip().rstrip(",")
                if not line or line.upper().startswith(
                        ("PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN KEY", "CONSTRAINT")):
                    continue
                cm = re.match(r'"?([a-z_][a-z0-9_]*)"?\s+\S', line, re.I)
                if cm and depth == 0:
                    cols.append(cm.group(1))
            tables.setdefault(tbl, [])
            for c in cols:
                if c not in tables[tbl]:
                    tables[tbl].append(c)
    return tables


def _dsn():
    env = os.environ.get("MNEMO_PG_DSN")
    if env:
        return env
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    envf = home / ".env"
    if envf.is_file():
        for line in envf.read_text().splitlines():
            if line.startswith("MNEMO_PG_DSN="):
                return line.split("=", 1)[1].strip().strip('"')
    sock = Path.home() / "lcm-pg" / "run"
    if sock.is_dir():
        return ("host=%s port=54329 dbname=lcm user=lcm01"
                " connect_timeout=5" % sock)
    raise SystemExit("converge_core_declared: no DSN (set MNEMO_PG_DSN)")


def main():
    import psycopg
    tables = core_tables()
    if not tables:
        raise SystemExit("converge_core_declared: core scan found 0 CREATE TABLEs")
    conn = psycopg.connect(_dsn(), autocommit=True)
    schemas = sys.argv[1:]
    if not schemas:
        with conn.cursor() as c:
            c.execute("select nspname from pg_namespace "
                      "where nspname like 'mnemosyne\\%' order by 1")
            schemas = [r[0] for r in c.fetchall()]
    created = added = 0
    for s in schemas:
        with conn.cursor() as c:
            c.execute("select nspname from pg_namespace where nspname=%s", (s,))
            if not c.fetchone():
                print("skip %s: schema absent" % s)
                continue
            existing = {}
            c.execute("select table_name, column_name from information_schema.columns "
                      "where table_schema=%s", (s,))
            for t, col in c.fetchall():
                existing.setdefault(t, set()).add(col)
            for tbl, cols in sorted(tables.items()):
                if tbl not in existing:
                    # find the original CREATE to run it (adapter rewrites dialect)
                    src = _find_create(tbl)
                    if src:
                        try:
                            # raw psycopg here (no adapter): pre-apply the same
                            # SQLite rewrites NativeConn does (native_conn.py
                            # ~1827: AUTOINCREMENT -> plain identity PK).
                            src = re.sub(
                                r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
                                "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
                                src, flags=re.I)
                            c.execute("SET search_path = %s, public" % _q(s))
                            c.execute(src)
                            created += 1
                            print("create %s.%s (core schema)" % (s, tbl))
                        except Exception as e:
                            print("create %s.%s FAILED: %s" % (s, tbl, str(e)[:120]))
                    continue
                have = existing[tbl]
                for col in cols:
                    if col in have:
                        continue
                    try:
                        c.execute("ALTER TABLE %s.%s ADD COLUMN IF NOT EXISTS %s text"
                                  % (_q(s), _q(tbl), _q(col)))
                        added += 1
                    except Exception as e:
                        print("add %s.%s.%s failed: %s" % (s, tbl, col, str(e)[:100]))
    conn.close()
    print("converge_core_declared: %d tables created, %d columns added"
          % (created, added))


_CREATE_ONE = {}


def _find_create(tbl):
    if tbl in _CREATE_ONE:
        return _CREATE_ONE[tbl]
    for py in sorted((REPO / "vendor" / "mnemosyne").rglob("*.py")):
        src = py.read_text(errors="replace")
        # non-greedy up to a line-final ")" (optional trailing comma) followed
        # by the closing triple-quote — stops at the real body end, never
        # overshoots into the following function definition.
        m = re.search(
            r'CREATE TABLE IF NOT EXISTS\s+"?%s"?\s*\(.*?^\s*\),?\s*"""' % re.escape(tbl),
            src, re.S | re.I | re.M)
        if m:
            _CREATE_ONE[tbl] = m.group(0).replace('"""', "").strip()
            return _CREATE_ONE[tbl]
    return None


def _q(name):
    if not re.fullmatch(r"\w+", name):
        raise SystemExit("unsafe identifier %r" % name)
    return '"%s"' % name


if __name__ == "__main__":
    main()
