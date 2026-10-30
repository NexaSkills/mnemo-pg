"""Bare-rowid -> shim-column rewriting for tables whose natural key is TEXT.

SQLite gives every table (without WITHOUT ROWID) an implicit integer
`rowid`. In PG the equivalent is the provisioned `<table>__rowid`
bigint identity shim. Core SQL that joins a vec companion on
`base.rowid` must therefore target `<table>__rowid` — but ONLY for
tables whose `rowid` is NOT a real column (episodic_memory genuinely
has a `rowid` column and must never be rewritten).
"""
import re

_BARE_ROWID_RE = re.compile(r"(?<![\w.\"])rowid\b", re.I)
_QUAL_ROWID_RE = re.compile(r"\b(\w+)\.rowid\b", re.I)

_REAL_ROWID: dict = {}
_EXISTS: dict = {}

_FROM_JOIN_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(?:\w+\s*\.\s*)?(\w+)"
    r"(?:\s+(?:AS\s+)?(?!WHERE\b|ON\b|SET\b|VALUES\b|USING\b|LEFT\b|RIGHT\b"
    r"|INNER\b|OUTER\b|CROSS\b|JOIN\b|GROUP\b|ORDER\b|LIMIT\b|LATERAL\b"
    r"|WHERE\b|RETURNING\b|CONFLICT\b|DO\b|BEGIN\b|FOR\b|\()\s*(\w+))?",
    re.I)


def _build_alias_map(sql: str) -> dict:
    """alias(lower) -> table for FROM/JOIN/UPDATE/INTO refs in the SQL."""
    out: dict = {}
    for m in _FROM_JOIN_RE.finditer(sql):
        t, a = m.group(1), m.group(2)
        out[t.lower()] = t
        if a:
            out[a.lower()] = t
    return out


def _table_exists(schema: str, table: str, conn) -> bool:
    key = (schema, table)
    hit = _EXISTS.get(key)
    if hit is not None:
        return hit
    try:
        row = conn.execute(
            "SELECT 1 FROM pg_class WHERE oid = "
            "cast(%s || '.' || %s as regclass) LIMIT 1",
            (schema, table)).fetchone()
        ok = row is not None
    except Exception:
        ok = False
    _EXISTS[key] = ok
    return ok


def _table_has_real_rowid(schema: str, table: str, conn) -> bool:
    key = (schema, table)
    hit = _REAL_ROWID.get(key)
    if hit is not None:
        return hit
    ok = False
    try:
        row = conn.execute(
            "SELECT 1 FROM pg_attribute WHERE attrelid = "
            "cast(%s || '.' || %s as regclass) AND attname = 'rowid' "
            "AND attnum > 0 AND NOT attisdropped", (schema, table)).fetchone()
        ok = row is not None
    except Exception:
        ok = True  # unknown table (missing): leave SQL untouched, fail loud
    _REAL_ROWID[key] = ok
    return ok


def rewrite_rowids(sql: str, schema: str, conn) -> str:
    """Retarget `<alias>.rowid` to `<base>__rowid` for tables lacking a
    real rowid column. Alias names are resolved against the FROM/JOIN
    clause. Conservative: on any doubt (missing table, unresolvable
    alias) the SQL is left untouched and fails loudly."""
    alias = _build_alias_map(sql)

    def qual(m):
        a = m.group(1)
        if a.lower() in ("new", "old", "excluded"):
            return m.group(0)
        # vec companion tables carry a REAL bigint rowid — never rewrite
        # qualifiers that resolve to a vec_* table (companion of anything).
        t0 = alias.get(a.lower()) or a
        if t0.lower().startswith("vec_") and _table_exists(schema, t0, conn):
            return m.group(0)
        t = alias.get(a.lower())
        if t is None:
            # qualifier is the table name itself, or unknown
            t = a if _table_exists(schema, a, conn) else None
        if t is None or _table_has_real_rowid(schema, t, conn):
            return m.group(0)
        return f"{a}.{t}__rowid"

    sql = _QUAL_ROWID_RE.sub(qual, sql)

    # Bare `rowid` (no qualifier): SQLite gives every table an implicit
    # rowid; core SQL like `SELECT rowid FROM working_memory WHERE ...`
    # relies on it.  Retarget ONLY statements whose FROM/UPDATE target is
    # a single shim-only table (no real rowid column).  Joins, subqueries,
    # INSERTs and mixed real/shim shapes are left untouched and fail
    # loudly as before.
    if _BARE_ROWID_RE.search(sql):
        m = re.match(
            r"\s*(?:SELECT\b.*?\bFROM|UPDATE)\s+(?:(\w+)\s*\.\s*)?(\w+)"
            r"(?:\s+(?:AS\s+)?(?!WHERE\b|SET\b|VALUES\b|USING\b|JOIN\b"
            r"|GROUP\b|ORDER\b|LIMIT\b|WHERE\b|RETURNING\b|CONFLICT\b"
            r"|DO\b|BEGIN\b|FOR\b|,|\()\s*(\w+))?",
            sql, re.I | re.S)
        if m and not re.search(r"\b(?:JOIN|UNION|INTERSECT|EXCEPT)\b"
                               r"|\bFROM\s*\(", sql, re.I):
            base = m.group(2).lower()
            if not base.startswith("vec_"):
                try:
                    known = base in {t for (_s, t) in _REAL_ROWID}
                except Exception:
                    known = False
                if known or _table_exists(schema, base, conn):
                    if not _table_has_real_rowid(schema, base, conn):
                        sql = _BARE_ROWID_RE.sub(f"{base}__rowid", sql)

    return sql
