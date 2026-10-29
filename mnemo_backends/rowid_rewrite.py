"""Alias-aware rowid rewriting for the PG backend.

SQLite gives every table an implicit `rowid`. The vendored core relies on it,
including in join shapes that alias the tables:

    LEFT JOIN vec_working vw ON vw.rowid = wm.rowid   (wm = working_memory)

The shim adds a `<table>__rowid` column to tables lacking a real rowid.
This module rewrites references so PG can resolve them:

  * `<alias>.rowid` where <alias> resolves (via `FROM|JOIN <table> [AS] <alias>`)
    to a shimmed (non-vec) table  ->  `"<table>"."<table>__rowid"`
    (this also covers vec-qualified joins like `vw.rowid`, which name the
    companion only as the join target; the content-table side is the key).
  * a bare `rowid` with no vec qualifier present  ->  shim of the single
    non-vec table in the statement.
  * `<alias>.rowid` resolving to a vec_* table (real rowid column) is left
    EXACTLY as written, so the paired rewrites stay consistent.

Consistency rule: if any qualifier resolves to a vec_* table (real column),
bare `rowid` mentions are NOT rewritten — vec_*.rowid is already valid PG and
half-rewriting a comparison `vw.rowid = wm.rowid` would break the join key.
"""

from __future__ import annotations

import re

_SHIM_SUFFIX = "__rowid"

_VEC_TABLES = {"vec_working", "vec_episodes", "vec_facts"}

_FROM_JOIN_RE = re.compile(
    r"(?i)\b(?:from|join|into|update)\s+([a-z_][a-z0-9_]*)"
    r"(?:\s+(?:as\s+)?([a-z_][a-z0-9_]*))?"
)
_AFTER_KEYWORDS = {
    "where", "on", "set", "values", "select", "order", "group", "limit",
    "offset", "using", "left", "right", "inner", "outer", "cross", "join",
    "returning", "having", "and", "or", "as", "natural", "full", "is",
    "conflict", "do", "with",
}


def _alias_map(sql: str) -> tuple[dict[str, str], set[str]]:
    """Return ({alias_or_table_lower: table_lower}, set(tables))."""
    amap: dict[str, str] = {}
    tables: set[str] = set()
    for m in _FROM_JOIN_RE.finditer(sql):
        t = m.group(1).lower()
        if t in ("select", "values", "lateral", "unnest"):
            continue
        tables.add(t)
        al = m.group(2)
        if al:
            al = al.lower()
            if al in _AFTER_KEYWORDS or al in tables:
                continue
            amap[al] = t
        amap[t] = t
    return amap, tables


def ensure_rowid_shims(conn) -> None:
    """Idempotent per-connection shim install."""
    if getattr(conn, "_mnemo_rowid_done", False):
        return
    from . import postgres  # noqa: F401
    cur = conn.cursor()
    cur.execute("CREATE SEQUENCE IF NOT EXISTS mnemo_rowid_seq")
    rows = cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n"
        " ON n.oid=c.relnamespace WHERE n.nspname = current_schema()"
        " AND c.relkind = 'r'").fetchall()
    for r in rows:
        t = r["relname"] if isinstance(r, dict) else r[0]
        if t.startswith("sqlite_") or t.endswith(_SHIM_SUFFIX) or t in _VEC_TABLES:
            continue
        has = cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE"
            " table_schema = current_schema() AND table_name = %s"
            " AND column_name = 'rowid' LIMIT 1", (t,)).fetchone()
        if has:
            continue
        cur.execute(
            f'ALTER TABLE "{t}" ADD COLUMN IF NOT EXISTS '
            f'"{t}{_SHIM_SUFFIX}" BIGINT DEFAULT nextval(\'mnemo_rowid_seq\')')
    conn._mnemo_rowid_done = True


def rewrite_rowid_sql(sql: str, known_tables: set[str] | None = None,
                      conn=None) -> str:
    """Rewrite rowid references against shim columns (alias-aware).

    `conn` (optional) is the live store connection: tables that already have
    a REAL `rowid` column (episodic_memory, vec_* companions) must keep
    `.rowid` references exactly as written — they resolve natively in PG.
    Without a conn the real-rowid set is probed via DSN or the vec-name rule.
    """
    if "rowid" not in sql.lower():
        return sql
    amap, tables = _alias_map(sql)
    if not tables:
        return sql

    # ---- fast path: conn-side checks first -------------------------------
    if conn is not None:
        real_map = {}
        for t in tables:
            if t in _VEC_TABLES:
                real_map[t] = True
            else:
                real_map[t] = _conn_has_real_rowid(conn, t)
    else:
        real = _real_rowid_tables()
        real_map = {t: (t in _VEC_TABLES or (real is not None and t in real))
                    for t in tables}

    def _is_real(t: str) -> bool:
        return bool(real_map.get(t))

    # Does any explicit qualifier resolve to a real-rowid table?
    real_qualified = False
    for m in re.finditer(r"(?i)\b([a-z_][a-z0-9_]*)\s*\.\s*rowid\b", sql):
        t = amap.get(m.group(1).lower())
        if t is not None and _is_real(t):
            real_qualified = True
            break

    # qualified mentions -> shim, ONLY for qualifiers resolving to shimmed tables
    def _qual_repl(m: re.Match) -> str:
        qual, t = m.group(1), amap.get(m.group(1).lower())
        if t is None or _is_real(t) \
                or (known_tables is not None and t not in known_tables):
            return m.group(0)
        # keep the ORIGINAL qualifier token: when the table is aliased in the
        # FROM (e.g. `episodic_memory em`), PG rejects table-qualified column
        # references — qualify through the alias instead.
        return f'"{qual}"."{t}{_SHIM_SUFFIX}"'

    sql = re.sub(r"(?i)\b([a-z_][a-z0-9_]*)\s*\.\s*rowid\b", _qual_repl, sql)

    # bare mentions: rewrite only if no real-rowid qualifier exists in the
    # statement (else the pair is already valid PG) and exactly one shimmed
    # table is in play.
    shimmed = {t for t in tables if not _is_real(t)}
    if known_tables is not None:
        shimmed = {t for t in shimmed if t in known_tables}
    if not real_qualified and len(shimmed) == 1:
        t = next(iter(shimmed))
        sql = re.sub(r"(?<![\w.\"])rowid\b", f'"{t}{_SHIM_SUFFIX}"', sql)
    return sql


_CONN_REAL_ROWID: dict[tuple[int, str], bool] = {}
_IN_ROWID_PROBE: set[int] = set()


def _conn_has_real_rowid(conn, table: str) -> bool:
    """Per-connection (per-adapter-object, per-table) real-rowid probe."""
    key = (id(conn), table)
    v = _CONN_REAL_ROWID.get(key)
    if v is not None:
        return v
    if id(conn) in _IN_ROWID_PROBE:
        # re-entrant probe (our own adapter hooks fire on this statement):
        # fall back to the vec-name rule, do not cache.
        return table in _VEC_TABLES
    _IN_ROWID_PROBE.add(id(conn))
    try:
        cur = conn.cursor()
        r = cur.execute(
            "SELECT 1 FROM pg_attribute a"
            " JOIN pg_class c ON c.oid = a.attrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = current_schema() AND c.relname = %s"
            " AND a.attname = 'rowid' AND a.attnum > 0"
            " AND NOT a.attisdropped LIMIT 1", (table,)).fetchone()
        v = r is not None
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        v = table in _VEC_TABLES  # safe fallback: names only
    finally:
        _IN_ROWID_PROBE.discard(id(conn))
    _CONN_REAL_ROWID[key] = v
    return v


_REAL_ROWID_CACHE: dict[str, set[str]] = {}


def _current_schema_of(conn) -> str | None:
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT current_schema()").fetchone()
        if row is None:
            return None
        return row["current_schema"] if isinstance(row, dict) else row[0]
    except Exception:
        return None


def _catalog_has_rowid_col(cur, table: str) -> bool:
    """True if the table (current_schema-resolved) has a real `rowid` column."""
    try:
        r = cur.execute(
            "SELECT 1 FROM pg_attribute a"
            " JOIN pg_class c ON c.oid = a.attrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = current_schema() AND c.relname = %s"
            " AND a.attname = 'rowid' AND a.attnum > 0"
            " AND NOT a.attisdropped LIMIT 1", (table,)).fetchone()
        return r is not None
    except Exception:
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return False


def _real_rowid_tables(schema: str | None = None) -> set[str] | None:
    """Tables in the current schema that have a real `rowid` column.

    Cached per schema for the process. Returns None when the catalog cannot
    be reached (callers then fall back to the vec-name rule only).
    """
    key = schema or ""
    cached = _REAL_ROWID_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        import os
        import psycopg
        dsn = os.environ.get("MNEMO_PG_DSN") or os.environ.get("LCM_PG_DSN") or ""
        if not dsn:
            return None
        with psycopg.connect(dsn, autocommit=True) as cx:
            rows = cx.execute(
                "SELECT c.relname FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " JOIN pg_attribute a ON a.attrelid = c.oid"
                " WHERE n.nspname = COALESCE(NULLIF(%s,''), current_schema())"
                " AND c.relkind = 'r' AND a.attname = 'rowid'"
                " AND a.attnum > 0 AND NOT a.attisdropped",
                (key,)).fetchall()
        out = {(r[0] if not isinstance(r, dict) else r["relname"]).lower()
               for r in rows}
        _REAL_ROWID_CACHE[key] = out
        return out
    except Exception:
        return None
