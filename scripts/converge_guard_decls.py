#!/usr/bin/env python3
"""converge_guard_decls — align existing columns to the core's guarded declarations.

The vendored core validates EVERY column it declares through
_add_column_if_missing: wrong type, NOT NULL, or DEFAULT => OperationalError
'schema mismatch for <table>.<col>: expected <decl>, got <actual>' => init dies
(_beam=None). converge_core_declared guarantees the columns EXIST; this pass
makes already-present columns PASS the core's validator.

Method (DRY): parse every literal _add_column_if_missing(conn, "t", "c", DECL)
call in vendor/mnemosyne/**/*.py. For each (table, col) present in a target
schema, compare against the core's DECL (TYPE [NOT NULL] [DEFAULT x]) and emit
the minimal fix:
  * type mismatch  -> ALTER COLUMN TYPE (USING cast; guarded to avoid data loss
                      via try/except — a failing cast skips the column loudly)
  * missing DEFAULT -> SET DEFAULT
  * TEXT <-> TIMESTAMP twins are accepted (core's own _column_matches allows
                      the pair) so timestamps are NOT churned.

Idempotent; never adds/drops columns; runs before the core's own init so the
validator sees a compliant schema.

Usage: converge_guard_decls.py [schema ...]   (default: every mnemosyne_*)
"""

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_ADDLIT_RE = re.compile(
    r'_add_column_if_missing\(\s*[\w.]+\s*,\s*"(\w+)"\s*,\s*"(\w+)"\s*,\s*"([^"]+)"')

# dict-driven guards:  { "col": "TYPE ...", ... } iterated with
# for col, ddl in {...}: _add_column_if_missing(conn, "table", col, ddl)
_DICTADD_RE = re.compile(
    r'(\{[^{}]*?\})\s*\.items\(\)\s*:\s*\n(?:[^\n]*\n){0,8}?\s*_add_column_if_missing\(\s*[\w.]+\s*,\s*"(\w+)"\s*,\s*(\w+)',
    re.S)

_SQLITE_PG = {"TEXT": "text", "INTEGER": "bigint", "REAL": "double precision",
             "BLOB": "bytea", "TIMESTAMP": "timestamp with time zone"}
# NB: guarded INTEGER columns (corrected_by at beam.py 2033/2037) arrive here
# through the literal-decl scan as INTEGER — no name-based override needed.


def guard_decls():
    """list of (table, column, expected_type, notnull, default_literal)."""
    out = []
    seen = set()
    for py in sorted((REPO / "vendor" / "mnemosyne").rglob("*.py")):
        src = py.read_text(errors="replace")
        for m in _ADDLIT_RE.finditer(src):
            tbl, col, decl = m.group(1), m.group(2), m.group(3)
            out.append(_parse_decl(tbl, col, decl))
        # dict-driven guards, two shapes:
        #  (a) for col, ddl in {...}.items(): _add_column_if_missing(..., table, col, ddl)
        for m in _DICTADD_RE.finditer(src):
            tbl = m.group(2)
            for dm in re.finditer(r'["\'](\w+)["\']\s*:\s*["\']([^"\']+)["\']', m.group(1)):
                key = (tbl, dm.group(1))
                if key not in seen:
                    seen.add(key)
                    out.append(_parse_decl(tbl, dm.group(1), dm.group(2)))
        #  (b) for col in [...]: _add_column_if_missing(..., table, col, {..}.get(col, 'TEXT'))
        for m in re.finditer(
                r'for\s+col\s+in\s+(\[[^\]]*\]).{0,400}?'
                r'_add_column_if_missing\(\s*[\w.]+\s*,\s*"(\w+)"\s*,\s*col\s*,\s*\{([^{}]*?)\}\.get',
                src, re.S):
            tbl = m.group(2)
            types = dict(re.findall(r'["\'](\w+)["\']\s*:\s*["\']([^"\']+)["\']', m.group(3)))
            for cm in re.finditer(r'["\'](\w+)["\']', m.group(1)):
                col = cm.group(1)
                key = (tbl, col)
                if key not in seen:
                    seen.add(key)
                    out.append(_parse_decl(tbl, col, types.get(col, "TEXT")))
    # dedupe keep-first
    ded, seen2 = [], set()
    for row in out:
        if (row[0], row[1]) not in seen2:
            seen2.add((row[0], row[1]))
            ded.append(row)
    return ded


def _parse_decl(tbl, col, decl):
    decl = decl.strip()
    notnull = bool(re.search(r"\bNOT NULL\b", decl, re.I))
    dm = re.search(r"\bDEFAULT\s+(.+)$", decl, re.I)
    default = dm.group(1).strip().rstrip(",") if dm else None
    tm = re.match(r"([A-Za-z]+)", decl)
    t = (tm.group(1).upper() if tm else "TEXT")
    return (tbl, col, t, notnull, default)


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
        return "host=%s port=54329 dbname=lcm user=lcm01 connect_timeout=5" % sock
    raise SystemExit("converge_guard_decls: no DSN (set MNEMO_PG_DSN)")


def _q(name):
    if not re.fullmatch(r"\w+", name):
        raise SystemExit("unsafe identifier %r" % name)
    return '"%s"' % name


_CAST_OK = {("text", "bigint"), ("text", "double precision"), ("bigint", "text"),
            ("double precision", "text"), ("text", "bytea"), ("bytea", "text"),
            ("bigint", "double precision"), ("double precision", "bigint"),
            ("timestamp with time zone", "text"), ("text", "timestamp with time zone"),
            ("integer", "bigint"), ("bigint", "integer")}


def main():
    import psycopg
    decls = guard_decls()
    conn = psycopg.connect(_dsn(), autocommit=True)
    schemas = sys.argv[1:]
    if not schemas:
        with conn.cursor() as c:
            c.execute("select nspname from pg_namespace where nspname like 'mnemosyne\\%' order by 1")
            schemas = [r[0] for r in c.fetchall()]
    fixed = skipped = 0
    for s in schemas:
        with conn.cursor() as c:
            c.execute("select t.relname, a.attname, "
                      "format_type(atttypid,atttypmod), attnotnull, "
                      "pg_get_expr(d.adbin, d.adrelid) "
                      "from pg_attribute a "
                      "join pg_class t on t.oid=a.attrelid "
                      "join pg_namespace n on n.oid=t.relnamespace "
                      "left join pg_attrdef d on d.adrelid=a.attrelid and d.adnum=a.attnum "
                      "where n.nspname=%s and t.relkind='r' and a.attnum>0 and not a.attisdropped",
                      (s,))
            present = {(t, col): (ty.lower(), nn, dflt)
                       for t, col, ty, nn, dflt in c.fetchall()}
            for tbl, col, want_type, want_nn, want_dflt in decls:
                have = present.get((tbl, col))
                if have is None:
                    continue  # existence handled by converge_core_declared
                htype, hnn, hdflt = have
                want = _SQLITE_PG.get(want_type, "text")
                if htype == want or {htype.split()[0], want.split()[0]} == {"text", "timestamp"} \
                        or (htype.startswith("timestamp") and want.startswith("timestamp")):
                    pass
                else:
                    if (htype, want) not in _CAST_OK:
                        print("skip type %s.%s.%s: %s -> %s (no safe cast)"
                              % (s, tbl, col, htype, want))
                        skipped += 1
                        continue
                    try:
                        c.execute("ALTER TABLE %s.%s ALTER COLUMN %s TYPE %s USING (%s::%s)"
                                  % (_q(s), _q(tbl), _q(col), want, _q(col), want))
                        print("type %s.%s.%s: %s -> %s" % (s, tbl, col, htype, want))
                        fixed += 1
                    except Exception as e:
                        conn.rollback() if not conn.autocommit else None
                        print("type %s.%s.%s FAILED (%s), keeping %s"
                              % (s, tbl, col, str(e)[:80], htype))
                        skipped += 1
                        continue
                # NOT NULL shape: relax only when the core does NOT require it
                # (a stricter DB than the core's own guard breaks the validator;
                # a looser one never does — the core inserts satisfy its own NOT NULL).
                if hnn and not want_nn:
                    try:
                        c.execute("ALTER TABLE %s.%s ALTER COLUMN %s DROP NOT NULL"
                                  % (_q(s), _q(tbl), _q(col)))
                        print("notnull %s.%s.%s -> drop" % (s, tbl, col))
                        fixed += 1
                    except Exception as e:
                        print("notnull %s.%s.%s FAILED: %s" % (s, tbl, col, str(e)[:80]))
                        skipped += 1
                # default shape: only enforce when core declares DEFAULT and PG has none
                if want_dflt and hdflt is None:
                    d = want_dflt
                    if d.upper() == "NULL":
                        pass
                    elif re.fullmatch(r"-?\d+(\.\d+)?", d):
                        c.execute("ALTER TABLE %s.%s ALTER COLUMN %s SET DEFAULT %s"
                                  % (_q(s), _q(tbl), _q(col), d))
                        print("default %s.%s.%s -> %s" % (s, tbl, col, d))
                        fixed += 1
                    elif re.fullmatch(r"'[^']*'", d):
                        c.execute("ALTER TABLE %s.%s ALTER COLUMN %s SET DEFAULT %s"
                                  % (_q(s), _q(tbl), _q(col), d))
                        fixed += 1
    conn.close()
    print("converge_guard_decls: %d fixes, %d skipped" % (fixed, skipped))


if __name__ == "__main__":
    main()
