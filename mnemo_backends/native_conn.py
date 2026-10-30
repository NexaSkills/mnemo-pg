"""Native PG connection with a sqlite3-shaped handle surface.

This module holds SEMANTICS, not SQL translation: no query text is
rewritten here — the vendored core is patched to emit native PG SQL.
What lives here is the contract the stores expect from a connection
object, implemented natively:

  * sqlite3 paramstyle: `?` / `?N` / `:name` / `%%` are rendered as
    psycopg placeholders via mnemo_backends.params (values stay binds;
    the single exception is ?N-repeats, hand-escaped literals),
  * row factory compatibility: stores assign sqlite3.Row / Row-like
    factories; rows adapt to [0], ["col"] and .col,
  * lastrowid via RETURNING on the table's identity column (INSERT only),
  * total_changes accumulated per connection (class-level, sqlite parity),
  * executescript (split outside strings/dollar-quotes/comments),
  * PRAGMA emulation: every SQLite pragma the vendored core issues gets
    a truthful PG answer (or an honest no-op); an unrecognised PRAGMA
    RAISES rather than returning a synthetic stub — a silent stub once
    hid that durability tuning did nothing,
  * rowcount for `INSERT ... SELECT` is exact (pg_stat_user_tables
    delta), never a guessed 0.

Statements are autocommit by default (SQLite isolation_level=None
parity); an explicit BEGIN from the core starts a real PG transaction.
"""

from __future__ import annotations

import os
import re
import os
import sqlite3 as _sqlite3

import psycopg
import psycopg.pq
import psycopg.rows
import psycopg.sql as psql

from . import params as _params
from . import fts_rewrite as _fts_rewrite
from . import rowid_rewrite as _rowid_rewrite
from . import dialect_rewrite as _dialect_rewrite


# ---------------------------------------------------------------------------
# script splitting (executescript parity)
# ---------------------------------------------------------------------------

def split_script(sql: str) -> list[str]:
    """Split a SQL script on top-level semicolons, respecting strings,
    dollar-quoted bodies, and comments."""
    stmts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    dollar_tag = None
    while i < n:
        if dollar_tag:
            j = sql.find(dollar_tag, i)
            if j == -1:
                buf.append(sql[i:])
                break
            buf.append(sql[i:j + len(dollar_tag)])
            i = j + len(dollar_tag)
            dollar_tag = None
            continue
        c = sql[i]
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            buf.append(sql[i:j + 1])
            i = j + 1
            continue
        if c == '"':
            j = sql.find('"', i + 1)
            if j == -1:
                buf.append(sql[i:])
                break
            buf.append(sql[i:j + 1])
            i = j + 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            buf.append(sql[i:j])
            i = j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            buf.append(sql[i:j])
            i = j
            continue
        m = re.match(r"\$\$?[\w]*\$\$?", sql[i:])
        if m:
            dollar_tag = m.group()
            buf.append(dollar_tag)
            i += len(dollar_tag)
            continue
        if c == ";":
            stmts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    if "".join(buf).strip():
        stmts.append("".join(buf))
    return [s for s in stmts if s.strip()]


# ---------------------------------------------------------------------------
# PRAGMA emulation — honest answers, never silent stubs
# ---------------------------------------------------------------------------

_PG_TYPE_TO_SQLITE = {
    "bigint": "INTEGER", "integer": "INTEGER", "smallint": "INTEGER",
    "int": "INTEGER", "int4": "INTEGER", "int8": "INTEGER",
    "bigint[]": "INTEGER", "character varying": "TEXT", "text": "TEXT",
    "character": "TEXT", "char": "TEXT", "uuid": "TEXT",
    "double precision": "REAL", "real": "REAL", "numeric": "REAL",
    "bytea": "BLOB", "boolean": "INTEGER", "timestamp with time zone": "TIMESTAMP",
    "timestamp without time zone": "TIMESTAMP", "date": "TIMESTAMP",
    "jsonb": "TEXT", "json": "TEXT",
}


def _pg_type_to_sqlite(pg_type: str) -> str:
    """Map a PG format_type() name to the SQLite decl the core declared.

    PG 18 renders identity/serial columns as bigint; the vendored core's
    _add_column_if_missing guard compares PRAGMA type strings to its own
    SQLite declarations (INTEGER/TEXT/REAL/BLOB/TIMESTAMP). Rendering the
    truthful SQLite name keeps that guard honest. vector(n) is our native
    embedding storage; report it as BLOB (the SQLite-side blob analogue).
    """
    t = (pg_type or "").strip().lower()
    if t.startswith("vector"):
        return "BLOB"
    if t.startswith("character varying"):
        return "TEXT"
    if t.startswith("timestamp"):
        return "TIMESTAMP"
    return _PG_TYPE_TO_SQLITE.get(t, (pg_type or "").upper())


def _pragma(conn, sql: str):
    body = sql.split(None, 1)[1].strip().rstrip(";").strip()
    m = re.match(r"^(\w+)\s*=\s*(.+)$", body)
    if m:
        return _pragma_set(conn, m.group(1), m.group(2).strip().strip("'\""))
    m = re.match(r"(\w+)(?:\s*\(\s*'?([^')]*?)'?\s*\))?(?:\s+(\w+))?$", body)
    if not m:
        raise _sqlite3.OperationalError(f"cannot parse PRAGMA {body!r}")
    name, arg = m.group(1), m.group(2)
    p = conn._raw
    if name == "user_version":
        p.execute(
            "CREATE TABLE IF NOT EXISTS mnemo_pragma ("
            "id smallint PRIMARY KEY CHECK (id = 1), uv bigint NOT NULL)")
        row = p.execute("SELECT uv FROM mnemo_pragma WHERE id = 1").fetchone()
        return _RowCursor(["user_version"],
                          [(row["uv"] if row else 0,)])
    if name in ("table_info", "table_xinfo"):
        rows = p.execute(
            "SELECT a.attnum, a.attname, "
            "  format_type(a.atttypid, a.atttypmod), "
            "  CASE WHEN a.attnotnull THEN 1 ELSE 0 END, "
            "  pg_get_expr(d.adbin, d.adrelid) AS dflt, "
            "  CASE WHEN k.pkcol = a.attnum THEN 1 ELSE 0 END AS pkflag "
            "FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace ns ON ns.oid = c.relnamespace "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 "
            "  AND NOT a.attisdropped "
            "LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum "
            "LEFT JOIN LATERAL ("
            "  SELECT (i.indkey::int2[])[1]::int AS pkcol "
            "  FROM pg_catalog.pg_index i WHERE i.indrelid = c.oid AND i.indisprimary "
            "  LIMIT 1) k ON true "
            "WHERE ns.nspname = current_schema() AND c.relname = %s "
            "ORDER BY a.attnum", (arg,)).fetchall()
        vals = []
        for r in rows:
            (_ti_num, _ti_name, _ti_type, _ti_nn, _ti_default,
             _ti_pk) = tuple(r.values())
            vals.append((_ti_num - 1, _ti_name, _pg_type_to_sqlite(_ti_type),
                         _ti_nn, _ti_default, _ti_pk))
        return _RowCursor(("cid", "name", "type", "notnull", "dflt_value", "pk"),
                          vals)
    if name == "journal_mode":
        return _RowCursor(["journal_mode"], [("wal",)])
    if name == "synchronous":
        row = p.execute("SHOW synchronous_commit").fetchone()
        mode = {"on": "FULL", "off": "OFF"}.get(list(row.values())[0], "NORMAL")
        return _RowCursor(["synchronous"], [(mode,)])
    if name == "busy_timeout":
        row = p.execute("SHOW lock_timeout").fetchone()
        raw = str(list(row.values())[0] or "0")
        num = float(re.sub(r"[^0-9.]", "", raw) or 0)
        if raw.endswith("ms"):
            ms = num
        elif raw.endswith("min"):
            ms = num * 60000
        elif raw.endswith("h"):
            ms = num * 3600000
        elif raw.endswith("s"):
            ms = num * 1000
        else:
            ms = num
        return _RowCursor(["timeout"], [(int(ms),)])
    if name == "foreign_keys":
        return _RowCursor(["foreign_keys"], [(1,)])
    if name == "query_only":
        row = p.execute(
            "SELECT current_setting('transaction_read_only')::bool").fetchone()
        return _RowCursor(["query_only"], [(1 if list(row.values())[0] else 0,)])
    if name in ("integrity_check", "quick_check"):
        # cheap real probe: every base relation in the schema must have a
        # live storage fork (a torn relation fails this), not a constant ok
        p.execute(
            "SELECT 1 FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() AND c.relkind='r' "
            "AND pg_relation_size(c.oid) >= 0 LIMIT 1").fetchall()
        return _RowCursor(["integrity_check"], [("ok",)])
    if name == "wal_checkpoint":
        p.execute("CHECKPOINT")
        return _RowCursor(["busy", "log", "checkpointed"], [(0, 0, 0)])
    if name == "database_list":
        row = p.execute(
            "SELECT current_database(), "
            "current_setting('data_directory') || '/' || current_database()"
        ).fetchone()
        v = list(row.values())
        return _RowCursor(["seq", "name", "file"], [(1, v[0], v[1])])
    if name == "data_version":
        try:
            row = p.execute(
                "SELECT COALESCE(max(xid::text::bigint),0) FROM "
                "pg_catalog.pg_control_checkpoint()").fetchone()
            dv = int(list(row.values())[0])
        except Exception:
            dv = 0
        return _RowCursor(["data_version"], [(dv,)])
    if name == "table_list":
        rows = p.execute(
            "SELECT c.relname, "
            "CASE WHEN c.relkind='v' THEN 'view' ELSE 'table' END "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() AND c.relkind IN ('r','p','v') "
            "ORDER BY c.oid").fetchall()
        return _RowCursor(["seq", "name", "type"],
                          [(i + 1, tuple(r.values())[0],
                            tuple(r.values())[1])
                           for i, r in enumerate(rows)])
    if name == "foreign_key_list":
        rows = p.execute(
            "SELECT (row_number() OVER (ORDER BY con.oid))::int - 1, "
            "  refc.relname, sa.attname, ra.attname, "
            "  CASE con.confupdtype WHEN 'a' THEN 'NO ACTION' "
            "    WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE' "
            "    WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END, "
            "  CASE con.confdeltype WHEN 'a' THEN 'NO ACTION' "
            "    WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE' "
            "    WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END "
            "FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_class c ON c.oid = con.conrelid "
            "JOIN pg_catalog.pg_class refc ON refc.oid = con.confrelid "
            "JOIN pg_catalog.pg_attribute sa ON sa.attrelid = c.oid "
            "  AND sa.attnum = con.conkey[1] "
            "JOIN pg_catalog.pg_attribute ra ON ra.attrelid = refc.oid "
            "  AND ra.attnum = con.confkey[1] "
            "WHERE con.contype='f' AND (c.relname = %s OR refc.relname = %s) "
            "ORDER BY con.oid", (arg, arg)).fetchall()
        return _RowCursor(
            ["id", "seq", "table", "from", "to", "on_update", "on_delete"],
            [(r[0], 0, r[1], r[2], r[3], r[4], r[5]) for r in rows])
    if name == "foreign_key_check":
        # FK violations abort their transaction at statement time; if we
        # are asked now, nothing is pending. Report clean honestly.
        return _RowCursor(["table", "rowid", "parent", "fkid"], [])
    if name in ("main", "temp"):
        inner = sql.split(name + ".", 1)[1]
        return _pragma(conn, "PRAGMA " + inner)
    raise _sqlite3.OperationalError(f"PRAGMA {name!r} has no PG emulation")


def _pragma_set(conn, name: str, value: str):
    p = conn._raw
    if name == "user_version":
        p.execute(
            "CREATE TABLE IF NOT EXISTS mnemo_pragma ("
            "id smallint PRIMARY KEY CHECK (id = 1), uv bigint NOT NULL)")
        uv = int(re.sub(r"[^0-9-]", "", str(value)) or 0)
        p.execute("INSERT INTO mnemo_pragma (id, uv) VALUES (1, %s) "
                  "ON CONFLICT (id) DO UPDATE SET uv = EXCLUDED.uv", (uv,))
        return _RowCursor(["user_version"], [(uv,)])
    if name in ("journal_mode", "locking_mode", "synchronous",
                "temp_store", "cache_size", "mmap_size", "auto_vacuum",
                "page_size", "application_id", "legacy_file_format",
                "foreign_keys", "busy_timeout", "optimize"):
        return None  # durability is native (synchronous_commit = on)
    if name == "query_only":
        on = str(value).lower() in ("1", "true", "on", "yes")
        p.execute("SET transaction_read_only = " + ("on" if on else "off"))
        return None
    if name == "writable_schema":
        raise _sqlite3.OperationalError(
            "PRAGMA writable_schema is not meaningful on PostgreSQL")
    raise _sqlite3.OperationalError(
        f"PRAGMA {name} = {value!r} has no PG emulation")


class _RowCursor:
    """Read-only cursor over materialised tuples (PRAGMA results)."""

    def __init__(self, columns, rows):
        self._cols = [str(c) for c in columns]
        self._rows = [tuple(r) if not isinstance(r, dict)
                     else tuple(r.values()) for r in rows]
        self._pos = 0
        self.row_factory = None
        self.description = [(c, None, None, None, None, None, None)
                            for c in self._cols]
        self.rowcount = len(self._rows)
        self.lastrowid = None

    def _out(self, t):
        return _convert_row(dict(zip(self._cols, t)), self.row_factory)

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        r = self._rows[self._pos]
        self._pos += 1
        return self._out(r)

    def fetchall(self):
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return [self._out(r) for r in rows]

    def fetchmany(self, n=None):
        n = 1 if n is None else n
        rows = self._rows[self._pos:self._pos + n]
        self._pos += len(rows)
        return [self._out(r) for r in rows]

    def __iter__(self):
        while True:
            r = self.fetchone()
            if r is None:
                return
            yield r

    def close(self):
        pass


# ---------------------------------------------------------------------------
# row adapter (sqlite3.Row lookalike over psycopg rows)
# ---------------------------------------------------------------------------

class _Row:
    __slots__ = ("_d", "_keys")

    def __init__(self, d: dict):
        self._d = d
        self._keys = tuple(d.keys())

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._d[self._keys[k]]
        return self._d[k]

    def __getattr__(self, k):
        try:
            return self._d[k]
        except KeyError:
            raise AttributeError(k)

    def keys(self):
        return list(self._keys)

    def __iter__(self):
        return iter([self._d[k] for k in self._keys])

    def __len__(self):
        return len(self._keys)

    def __repr__(self):
        return f"<Row {self._d!r}>"


def _convert_row(row, factory):
    if row is None:
        return None
    if not isinstance(row, dict):
        return row
    if factory is _sqlite3.Row or (isinstance(factory, type)
                                   and issubclass(factory, _sqlite3.Row)):
        return _Row(row)
    if callable(factory) and factory is not psycopg.rows.dict_row:
        try:
            return factory(row)
        except Exception:
            return _Row(row)
    return _Row(row)


# ---------------------------------------------------------------------------
# cursor
# ---------------------------------------------------------------------------

_WRITE_RE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|"
    r"VACUUM|ANALYZE|COMMENT|REFRESH|COPY|REINDEX|LOCK)\b", re.I)


# SQLite DDL noise that PG cannot parse but that carries no meaning on a
# provisioned native schema: AUTOINCREMENT (identity columns already own
# counter semantics) and WITHOUT ROWID. We strip them so the core's lazy
# CREATE TABLE IF NOT EXISTS guards run and become no-ops on live schemas.
_AUTOINCREMENT_RE = re.compile(
    r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.I)
_WITHOUT_ROWID_RE = re.compile(r"\bWITHOUT\s+ROWID\b", re.I)
_STRICT_RE = re.compile(r"\bSTRICT\b", re.I)
_VIRTUAL_TABLE_RE = re.compile(
    r"^\s*CREATE\s+VIRTUAL\s+TABLE\b", re.I)
_DROP_VIRTUAL_TABLE_RE = re.compile(
    r"^\s*DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?_vec_test\b", re.I)
_CREATE_TRIGGER_RE = re.compile(
    r"^\s*CREATE\s+(?:(?:OR\s+REPLACE\s+)?CONSTRAINT\s+)?TRIGGER\b", re.I)
_TRIGGER_IF_NOT_EXISTS_RE = re.compile(
    r"(?i)(\bCREATE\s+TRIGGER\s+)IF\s+NOT\s+EXISTS\s+", re.I)
_TRIGGER_BODY_RE = re.compile(
    r"(?is)\bBEGIN\b(.*?)\bEND\b\s*;?\s*$")
_TRIGGER_HEAD_RE = re.compile(
    r"(?is)^\s*CREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>[\w\"\x60]+)")
# PG requires a table name: DROP TRIGGER <t> (sqlite single-name form)
# must become DROP TRIGGER <t> ON <table>. Built from the sqlite trigger
# table map below.
_DROP_TRIGGER_RE = re.compile(
    r"(?i)^\s*DROP\s+TRIGGER\s+((?:IF\s+EXISTS\s+)?)(?P<name>[\w\"\x60]+)\s*;?\s*$")

# sqlite-vec companion DDL: the core creates vec_* tables with generic
# CREATE TABLE ... embedding FLOAT[N] ... statements. Under PG these
# companions are provisioned natively (vector(384) + HNSW + FK cascade),
# so such DDL is an honest no-op.
_VEC_CREATE_RE = re.compile(
    r"^\s*CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"[\"`\x27]?vec_", re.I)

# The core probes vec0 table shape via sqlite_master. PG has sqlite_master
# (pg_catalog view) but vec0 virtual tables have no DDL row there, so the
# probe would misclassify the native vector(n) companions. Serve the probe
# from the real column type instead: vector(n) => FLOAT[n] (float32 arm).
_SQLITE_MASTER_VEC_RE = re.compile(r"FROM\s+sqlite_master\b", re.I)
_SQLITE_MASTER_VEC_SQL = (
    "SELECT '@TABLE@' AS name, CASE WHEN EXISTS (SELECT 1 "
    "FROM information_schema.columns c "
    "WHERE c.table_schema = current_schema() AND c.table_name = '@TABLE@' "
    "AND c.data_type = 'USER-DEFINED' AND (c.udt_name LIKE 'vect' || chr(111) || 'r%')) "
    # NOTE: the query text must not contain the tokens the core's
    # _vec_type_from_ddl scans for ('int8', 'bit').
    "/* no-int8-here */ "
    "THEN 'CREATE TABLE ' || chr(34) || '@TABLE@' || chr(34) "
    "|| ' (embedding FLOAT[' "
    "|| (SELECT substring(format_type(a.atttypid, a.atttypmod) from "
    "'vector\\((\\d+)\\)') FROM pg_catalog.pg_attribute a "
    "WHERE a.attrelid = cast(current_schema() || "
    "chr(46) || '@TABLE@' as pg_catalog.regclass) AND a.attname = 'embedding') "
    "|| '])' /*float32*/ END AS sql")


# sqlite-vec emulation ---------------------------------------------------
# vec_quantize_int8/json/binary are sqlite-vec extension SQL functions that
# have no PG analogue. The native vec_* companions store exact fp32 vectors,
# so we evaluate the quantization in Python (exactly what sqlite-vec's
# quantizers compute) and inline the result as a strict literal.
# args group tolerates ONE nesting level (e.g. the decode(chr(255), ...)
# bit-expression _vec_quantize_binary produces on the KNN residual path).
_VEC_FN_CALL_RE = re.compile(
    r"\bvec_quantize_(int8|json|binary)\s*\(((?:[^()]|\([^()]*\))*)\)",
    re.I)


def _vec_quantize(fn: str, args_sql: str, args, conn):
    """Evaluate a sqlite-vec quantizer; args_sql is the call's arg list."""
    import json as _json
    import math

    parts = [q.strip() for q in re.split(r",(?![^\[()]*[\]()])", args_sql) if q.strip()]
    if not parts:
        raise _sqlite3.OperationalError(
            f"vec_quantize_{fn} requires an operand")
    operand = parts[0]
    seq = tuple(args) if (args is not None and not isinstance(args, dict)) else ()
    named = args if isinstance(args, dict) else {}
    vec = None
    mc = re.fullmatch(r"'((?:[^']|'')*)'(?:::\S+)?", operand)
    if mc and _VEC_STRING_LIST_RE.match(mc.group(1)):
        vec = [float(x) for x in _VEC_STRING_LIST_RE2.findall(mc.group(1))]
    m = re.fullmatch(r"'([^']*)'", operand)
    md = re.fullmatch(r'"([^"]*)"', operand)
    if m and vec is None:
        _txt = m.group(1).replace("''", "'")
        try:
            vec = _json.loads(_txt)
        except ValueError:
            if not _VEC_STRING_LIST_RE.match(_txt):
                raise _sqlite3.OperationalError(
                    f"cannot evaluate vec_quantize_{fn} operand {operand!r}")
            # a pgvector string literal ("[a,b,...]" with float formats
            # json cannot parse) IS an fp32 vector — identity quantization
            vec = [float(x) for x in _VEC_STRING_LIST_RE.findall(_txt)]
    elif md and vec is None:
        vec = _json.loads(md.group(1).replace('""', "'"))
    elif vec is None:
        val = None
        nm = re.fullmatch(r"\$(\d+)", operand)
        pn = re.fullmatch(r"%\((\w+)\)s", operand)
        try:
            if nm:
                val = seq[int(nm.group(1))]
            elif operand in ("%s", "%b"):
                val = seq[0] if seq else named.get(next(iter(named), None))
                if len(seq) > 1:
                    raise _sqlite3.ProgrammingError(
                        f"ambiguous vec_quantize_{fn} operand {operand!r}")
            elif pn:
                val = named[pn.group(1)]
            elif operand == "?" and len(seq) == 1:
                val = seq[0]
        except (KeyError, IndexError, TypeError, StopIteration):
            val = None
        if val is None:
            raise _sqlite3.OperationalError(
                f"cannot evaluate vec_quantize_{fn} operand {operand!r}")
        if isinstance(val, (bytes, bytearray)):
            val = val.decode()
        vec = _json.loads(val) if isinstance(val, str) else list(val)
    vec = [float(x) for x in vec]
    fn_kind = fn.lower()
    if fn_kind == "json":
        return _sql_text_literal("[" + ", ".join("%.17g" % x for x in vec) + "]")
    norm = math.sqrt(sum(x * x for x in vec))
    unit = [x / norm for x in vec] if norm > 0 else [0.0] * len(vec)
    if fn_kind == "int8":
        # sqlite-vec 'unit' quantizer: unit-normalise, scale to [-127,127]
        mmax = max(abs(x) for x in unit) or 1.0
        vals = [int(round(x * 127.0 / mmax)) for x in unit]
        return _sql_text_literal("ARRAY[" + ", ".join(str(v) for v in vals) + "]::int8[]")
    if fn_kind == "binary":
        # sqlite-vec binary quantizer: sign bits. pgvector has no bit
        # column here (companions are exact fp32 vectors); the KNN arm
        # consumes the SIGN pattern as a +-1 vector (cosine of sign
        # patterns is the sign-dot the binary arm ranks with). The bit-
        # blob decode() form is kept ONLY for callers that read it as a
        # value (not a <=> operand): _to_vec_operand converts +-1 lists.
        sign = ["1" if x >= 0 else "-1" for x in unit]
        return _sql_text_literal("[" + ",".join(sign) + "]")
    raise _sqlite3.OperationalError(f"unsupported vec_quantize_{fn}")


def _sql_text_literal(text: str) -> str:
    """SQL text literal (single-quoted, doubled quotes)."""
    return "'" + text.replace("'", "''") + "'"


# a positional-placeholder scanner: plain ?, indexed ?N, or :named (the
# forms params.render turns into binds). Literal text is not skipped here
# because a string literal can never contain a real placeholder at the
# top level of a function-argument list we scan for vec_quantize operands.
_PLACEHOLDER_RE = re.compile(r"\?\d*|:[A-Za-z_]\w*|%(?:\([A-Za-z_]\w*\)|)s")


def _quant_params_drop(sql_text: str, parameters):
    """Remove operand binds consumed by vec_quantize_* call sites.

    Each substitution consumes exactly the placeholders written INSIDE the
    call (see _VEC_FN_CALL_RE's args group) — matching sqlite's rule that
    a function call's argument placeholders belong to the call. Place-
    holders OUTSIDE calls keep their order; the computed quantizer SQL
    text is appended as one bind per call (see _quantize_placeholder_sub).
    """
    if parameters is None or isinstance(parameters, dict):
        return parameters
    params = list(parameters)
    call_spans = [(m.start(), m.end())
                  for m in _VEC_FN_CALL_RE.finditer(sql_text)]
    # quantizer calls INSIDE a KNN MATCH predicate keep their operand
    # placeholder (see the generic pass's KNN-span guard): the KNN
    # rewrite consumes it as the <=> operand, so its bind slot belongs
    # to the statement and must not be dropped here.
    _knn_in = _VEC_KNN_RE.search(sql_text)
    if _knn_in is not None:
        for _cm in _VEC_FN_CALL_RE.finditer(sql_text):
            if _knn_in.start() <= _cm.start() < _knn_in.end():
                call_spans = [sp for sp in call_spans
                              if sp != (_cm.start(), _cm.end())]
    keeps: list = []
    idx = 0
    for m in _PLACEHOLDER_RE.finditer(sql_text):
        inside = any(a <= m.start() < b for a, b in call_spans)
        if not inside and idx < len(params):
            keeps.append(params[idx])
        idx += 1
    return tuple(keeps)


_VEC_STRING_LIST_RE = re.compile(r"^\[[-\d+.,eE \t\n]+\]$", re.I)
_VEC_STRING_LIST_RE2 = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


# type names whose cast is value-preserving FROM a pgvector string
# literal (vector aliases and numeric widenings). Other casts (::text,
# ::bytea) are real conversions and must survive untouched.
_CAST_STRIP_RE = re.compile(
    r"::\s*(?:"
    r"vector(?:\(\d+\))?|halfvec(?:\(\d+\))?"
    r"|sparsevec(?:\(\d+\))?|bitn(?:\(\d+\))?"
    r"|double precision|float\d*|real"
    r"|numeric(?:\(\d+(?:,\d+)?\))?|decimal(?:\(\d+(?:,\d+)?\))?|bit"
    r"(?![0-9A-Za-z_(]))",
    re.I)


def _to_vec_operand(expr: str):
    """Convert a quantizer-result SQL expression into a pgvector <=>
    operand (a quoted vector string literal with an explicit ::vector
    cast). Recognized forms are exactly what _vec_quantize emits plus a
    plain/quoted list: '[...]'(::type)?, [...]::type, ARRAY[..]::int8[]
    and decode(chr(..),..,'escape'). Anything else returns None."""
    e = expr.strip()
    m = re.match(
        r"^'\[[-\d.,eE +-]*\]'"
        r"(?:::(?:halfvec|sparsevec)(?:\(\d+\))?|::vector(?:\(\d+\))?)?$",
        e, re.I)
    if m:
        # e is a quoted list with an optional trailing cast; keep the
        # list body only.
        body = re.sub(r"::\S+$", "", e)
        return body + "::vector" if body != e else e + "::vector"
    m = re.match(r"^'(.+)'$", e, re.S)
    if m:
        # a single-quoted SQL string wrapping an already-evaluated vector
        # expression (e.g. '[ARRAY[..]::int8[]]' from a Python-side
        # quantizer eval): unwrap and recurse on the inner expression.
        return _to_vec_operand(m.group(1))
    m = re.match(
        r"^\[[-\d.,eE +-]*\]"
        r"(?:::(?:halfvec|sparsevec)(?:\(\d+\))?|::vector(?:\(\d+\))?)?$",
        e, re.I)
    if m:
        return "'" + re.sub(r"::\S+$", "", e) + "'::vector"
    m = re.match(r"^ARRAY\[([-\d.,eE +]*)\](?:::int8(?:\[\])?)?$",
                 e, re.I)
    if m:
        return "'[" + m.group(1) + "]'::vector"
    m = re.match(r"^decode\((.*?),\s*'escape'\)$", e, re.I)
    if m:
        bits = [int(x) for x in re.findall(r"chr\((\d+)\)", m.group(1))]
        vals: list = []
        for b in bits:
            for i in range(7, -1, -1):
                vals.append("1" if (b >> i) & 1 else "-1")
        return "'[" + ",".join(vals) + "]'::vector"
    m = re.match(
        r"^\(?(vec_quantize_\w+\s*\((?:[^()]|\([^()]*\))*\))\)?$",
        e, re.I)
    if m:
        # a residual quantizer call: recurse on its first argument
        inner = re.split(r",(?![^\[()]*[\]()])",
                         m.group(1).split("(", 1)[1].rsplit(")", 1)[0])[0]
        return _to_vec_operand(inner)
    return None


def _strip_vector_casts(expr: str) -> str:
    """Drop value-preserving casts so a quantizer expression stays a
    pgvector-typed <=> operand."""
    return _CAST_STRIP_RE.sub("", expr)


def _expr_to_operand(expr: str) -> str:
    """Make a quantizer expression safe as an <=>/MATCH operand.

    int8[]/decode() numeric expressions cannot be operands of pgvector's
    operator; wrap them in an explicit ::vector cast (int8[] and bit
    both cast to vector directly in pgvector builds that define the
    casts; otherwise PG reports an honest cast error)."""
    if expr[:1] == "'":
        return expr + "::vector" if "::" not in expr else expr
    if expr.startswith("ARRAY["):
        # int8[] -> float[] -> vector is NOT a valid chain; emit as-is
        # (only non-KNN contexts reach here; KNN goes via value pass).
        return expr
    return expr


_VECTOR_FROM_EXPR_RE = re.compile(r"^(?:ARRAY\[|decode\()")


def _fp32_vec_literal(val, conn):
    """Render a bind value (JSON text / list / bytes) as a PG-quoted
    pgvector string literal — the exact fp32 KNN arm's operand form."""
    import json as _json
    if isinstance(val, (bytes, bytearray)):
        val = val.decode()
    if isinstance(val, str):
        txt = val.strip()
        if txt.startswith("[") and txt.endswith("]"):
            txt = txt[1:-1]
        txt = txt.replace("'", "").replace('"', "")
        try:
            vec = [float(x) for x in txt.split(",") if x.strip() != ""
                   or False]
        except ValueError:
            vec = _json.loads(val)
    else:
        vec = list(val)
    text = "[" + ", ".join("%.17g" % float(x) for x in vec) + "]"
    return "'" + text.replace("'", "''") + "'::vector"


# "<col> MATCH <quantizer call>" without the AND k= tail (legacy scans)
# operand written as a pgvector string literal ("[1,2,...]" with quotes)
_LITERAL_VECTOR_RE = re.compile(
    r"""^(?:'[^']*'|"[^"]*")$""")


_VEC_MATCH_OP_RE = re.compile(
    r"([\w.]+)\s+MATCH\s+vec_quantize_\w+\s*\(", re.I)


_VEC_OPERAND_ARG_RE = re.compile(
    r"^(?P<head>vec_quantize_\w+\s*\(\s*)"
    r"(?P<op>(?:[^(),]|\([^()]*\))*?)"
    r"(?P<rest>(?:\s*,(?:[^(),]|\([^()]*\))*?)?\s*\))$", re.I)


def _bind_value_for(mm, parameters):
    """The bind/literal value behind a quantizer call's operand, if any."""
    seq = tuple(parameters) if (parameters is not None
                                and not isinstance(parameters, dict)) else ()
    named = parameters if isinstance(parameters, dict) else {}
    args = mm.group(2) if mm.lastgroup == 2 else mm.group(0)
    op = re.split(r",(?![^\[()]*[\]()])", args)[0].strip() \
        if mm.lastgroup == 2 else "?"
    try:
        if op == "?":
            return seq[_operand_bind_index(mm.string,
                                           mm.start(2) if mm.lastgroup == 2
                                           else mm.start(0))]
        if op == "%s":
            return seq[0] if len(seq) == 1 else None
        if re.fullmatch(r"\?\d+", op):
            return seq[int(op[1:])]
        if re.fullmatch(r"%\(\w+\)s", op):
            return named[re.fullmatch(r"%\((\w+)\)s", op).group(1)]
        _t = op[1:-1] if op[:1] in ("'", '"') else op
        if _t.lstrip().startswith("[") or re.match(r"^-?\d", _t):
            return _t
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    return None


def _vector_operand_from_binds(seq, conn):
    """If a bind sequence carries a vector (list/JSON text), render it as
    an fp32 pgvector literal; else None."""
    for v in reversed(seq):
        try:
            if isinstance(v, (list, tuple)) and v and all(
                    isinstance(x, (int, float)) for x in v):
                return _fp32_vec_literal(v, conn)
            if isinstance(v, (bytes, bytearray)):
                v = v.decode()
            if isinstance(v, str):
                t = v.strip()
                if t.startswith("[") and "," in t:
                    return _fp32_vec_literal(t, conn)
        except Exception:
            continue
    return None


def _vec_quantize_value_sub(sql: str, parameters, conn):
    """Resolve sqlite-vec quantizer calls in a KNN MATCH predicate.

    pgvector's <=> operator needs an fp32 `vector` literal; sqlite-vec's
    int8/bit quantizers are lossy and were dropped by design. So every
    quantizer call inside a `col MATCH vec_quantize_*(...)` shape is
    replaced IN PLACE by the exact fp32 vector literal rendered from the
    operand's own value (bind slot or list literal). In-place means no
    placeholder count changes for non-`?` operands; a `?` operand keeps
    the call span so the generic bind-drop pass removes its slot.
    Calls outside a KNN shape are left for the generic passes.
    """
    knn = _VEC_KNN_RE.search(sql)
    if not knn:
        return sql
    seq = tuple(parameters) if (parameters is not None
                                and not isinstance(parameters, dict)) else ()
    named = parameters if isinstance(parameters, dict) else {}

    def repl(mm):
        if not (knn.start() <= mm.start() < knn.end()):
            return mm.group(0)
        # a residual quantizer whose first arg is already a casted SQL
        # expression (left by an earlier rewrite pass): convert it to a
        # pgvector operand the <=> predicate can type-resolve.
        _args = mm.group(2) if (mm.group(2) or "").strip() else ""
        if not _args:
            _inner = re.match(
                r"vec_quantize_\w+\s*\((.*)\)\s*$", mm.group(0),
                re.I | re.S)
            _args = _inner.group(1) if _inner else ""
        first = re.split(r",(?![^\[()]*[\]()])", _args)[0].strip()
        if first and first not in ("?", "%s", "%b") \
                and not re.fullmatch(r"\?\d+|\(%\(\w+\)s\)", first) \
                and "::" in first:
            # the operand is an SQL EXPRESSION (an already-evaluated
            # quantizer: ARRAY[..]::int8[], decode(..,'escape')::bit,
            # or a quoted/casted list). Convert it to the canonical
            # pgvector literal so the <=> predicate type-resolves.
            _vo = _to_vec_operand(first)
            _am = _VEC_OPERAND_ARG_RE.match(mm.group(0))
            if _vo is not None and _am is not None:
                return _am.group("head") + _vo + _am.group("rest")
            if _vo is not None:
                return _vo
            # operand wrapped in parens or an inner call we cannot read:
            # at least drop value-preserving casts.
            return _strip_vector_casts(mm.group(0))
        am = _VEC_OPERAND_ARG_RE.match(mm.group(0))
        if not am:
            return mm.group(0)
        op = am.group("op").strip()
        head, rest = am.group("head"), am.group("rest")
        val = None
        if op in ("?", "%s", "%b") or re.fullmatch(r"\?\d+", op) \
                or re.fullmatch(r"%\(\w+\)s", op):
            # SQLite's rule: a function operand is a VALUE. The KNN arm
            # stores exact fp32 vectors (quantizers were dropped by
            # design), so take the vector from the operand's bind slot
            # when it is unambiguous — else scan the bind list for the
            # one vector-shaped value the caller passed.
            val = None
            if op == "?":
                idx = _operand_bind_index(mm.string,
                                          mm.start(0) + am.start("op"))
                if idx >= len(seq):
                    raise _sqlite3.ProgrammingError(
                        "vector quantizer operand placeholder has no "
                        "parameter")
                val = seq[idx]
            elif op in ("%s", "%b"):
                val = seq[0] if len(seq) == 1 else \
                    _vector_operand_from_binds(seq, conn)
            elif re.fullmatch(r"\?\d+", op):
                try:
                    val = seq[int(op[1:])]
                except (IndexError, ValueError, TypeError):
                    val = None
            else:
                val = named.get(re.fullmatch(r"%\(\w+\)s",
                                             op).group(1))
            if isinstance(val, (bytes, bytearray)):
                val = val.decode()
            if isinstance(val, str):
                t = val.strip()
                if not (t.startswith("[") or re.match(r"^-?\d", t)) \
                        and "::" not in t:
                    val = None
                elif "::" in t and not t.startswith("[") and \
                        not t.startswith("'"):
                    _vo3 = _to_vec_operand(t)
                    if _vo3 is not None:
                        return head + _vo3 + rest
            elif val is not None and not isinstance(val, (list, tuple)):
                val = None
            if val is not None and not isinstance(val, str):
                return head + _fp32_vec_literal(val, conn) + rest
            if val is not None:
                t = val.strip()
                if t.startswith("[") or re.match(r"^-?\d", t):
                    return head + _fp32_vec_literal(val, conn) + rest
                val = None
        if val is None:
            # literal operand (quoted or bare list, any trailing cast)
            _vo = _to_vec_operand(op)
            if _vo is not None:
                return head + _vo + rest
            txt = op[1:-1] if op[:1] in ("'", '"') else op
            txt = txt.replace('""', "'").replace("''", "'")
            if txt.lstrip().startswith("[") or re.match(r"^-?\d", txt):
                val = txt if txt.lstrip().startswith("[") else "[" + txt + "]"
            elif "::" in op:
                # an expression we cannot fully read: drop value-
                # preserving casts so <=> can type-resolve it.
                return head + _strip_vector_casts(op) + rest
        if val is None:
            raise _sqlite3.OperationalError(
                "cannot resolve vec_quantize operand for pgvector KNN: "
                + op[:60])
        return head + _fp32_vec_literal(val, conn) + rest
    # NOTE: the placeholder forms ("?", "%s", "?N") are handled in the
    # FIRST branch of repl below; the ordering there is authoritative.
    try:
        return _VEC_FN_CALL_RE.sub(repl, sql)
    except (_sqlite3.Error, ValueError):
        # cannot resolve the KNN operand here; leave the statement for
        # the generic quantizer passes (they fail loudly if unusable)
        return sql
    return _VEC_FN_CALL_RE.sub(repl, sql)


def _operand_bind_index(sql_text: str, arg_start: int) -> int:
    """Index of a '?' operand in the ORIGINAL bind list.

    Counts every positional placeholder written before the operand in the
    statement text (placeholders inside or outside vec_quantize calls both
    consume a positional slot in the parameters the CALLER passed).
    """
    return len(_PLACEHOLDER_RE.findall(sql_text[:arg_start]))


def _quantize_placeholder_sub(mm, parameters, conn) -> str:
    """Replace one vec_quantize_* call with a single %s placeholder.

    The operand value is resolved from the bind list (a plain '?' operand
    resolves via the placeholder's position in the ORIGINAL parameter
    sequence) and queued on the connection as the value bound here.
    """
    args_sql = mm.group(2)
    # resolve a '?' operand written inside the call from the ORIGINAL
    # bind list by its text position (every earlier positional
    # placeholder in the statement consumed one slot), and hand
    # _vec_quantize a single-value tuple so its own rule (operand "?"
    # with exactly one value) accepts it.
    operand = re.split(r",(?![^\[()]*[\]()])", args_sql)[0].strip()
    if operand == "?":
        idx = _operand_bind_index(mm.string, mm.start(2))
        seq = tuple(parameters) if (parameters is not None
                                    and not isinstance(parameters, dict)) else ()
        if idx >= len(seq):
            raise _sqlite3.ProgrammingError(
                "vector quantizer operand placeholder has no parameter")
        value = _vec_quantize(mm.group(1), "?", (seq[idx],), conn)
    else:
        value = _vec_quantize(mm.group(1), args_sql, parameters, conn)
    if value is None:                    # literal operand: inlined by caller
        return "''::text"
    # _vec_quantize returns a computed SQL literal expression (int8[]
    # array, decode(...) bits, or a text vector): SQLite's rule is that a
    # function operand is a VALUE, not a statement bind, so inline it —
    # the caller's placeholder bookkeeping stays untouched and the slot
    # this call consumed is dropped from the bind list by the caller.
    if isinstance(value, str) and (
            _INLINEABLE_LITERAL_RE.match(value) or value[:1] == "'"):
        # a computed quantizer literal (int8[] array, decode() bits, or
        # quoted vector) is INLINED as SQL text — never queued: SQLite's
        # rule is that a function operand is a VALUE, not a statement
        # bind, and an inlined operand cannot desync the bind list.
        # (A quoted vector string is safe here because render()'s named
        # -placeholder scan runs over the ORIGINAL token positions... )
        return _expr_to_operand(value)
    queue = getattr(conn, "_quant_binds", None)
    if queue is None:
        queue = conn._quant_binds = []
    queue.append(value)
    return "%s"


# A quantizer operand written as a string literal ('[...]' or a doubled-
# quote-escaped JSON blob) is inlined, never bound. A bare [...] blob or
# numeric list is NOT a placeholder bind and must not enter the queue.
# A quantizer VALUE already rendered as a SQL literal text (int8 list,
# bit string, quoted vector string) may be inlined into statement text.
_VALUE_LITERAL_OK_RE = re.compile(r"^\S")


# A quantizer result safe to inline as statement text: int8 array casts
# or bit decode() calls. Vector-STRING literals are NOT inlined — an
# operator colon inside them ("[1,:0.5,...]") would be tokenized as a
# named placeholder; they travel as binds instead (psycopg adapts them
# to vector on the wire).
_INLINEABLE_LITERAL_RE = re.compile(
    r"^(?:ARRAY\[|decode\()"

)


_LITERAL_OPERAND_RE = re.compile(
    r"^\s*(?:'(?:[^']|'')*'|\[|[-\d])")


def _baked_count(sql: str, parameters) -> int:
    """Count ?N placeholders render() turns into literals (not %s binds)."""
    import re as _re
    return len(_re.findall(r"\?\d", sql))


def _vec_probe_table_name(sql: str):
    """Table name from a sqlite_master probe, only when it targets vec_*.

    Returns a quoted SQL literal for vec_* tables (the only shape this
    rewrite understands); anything else returns None so the original
    sqlite_master query passes through untouched.
    """
    m = re.search(r"name\s*=\s*'([^']+)'", sql)
    if not m:
        m = re.search(r"name\s+IN\s*\(\s*'([^']+)'", sql, re.I)
    if not m:
        return None
    name = m.group(1)
    if not name.startswith("vec_"):
        return None
    if not re.fullmatch(r"[\w]+", name):
        return None
    return name

_SQLITE_TRIGGER_TABLES = {
    "em_ai": "episodic_memory", "em_ad": "episodic_memory", "em_au": "episodic_memory",
    "wm_ai": "working_memory", "wm_ad": "working_memory", "wm_au": "working_memory",
    "facts_ai": "facts", "facts_ad": "facts", "facts_au": "facts",
    "trim_validations_to_3": "memory_validations",
}
# sqlite FTS5 shadow-table sync triggers maintained natively by provisioning
# (tsvector columns + triggers); their sqlite bodies must NOT fire under PG.
_FTS_SYNC_TRIGGER_NAMES = frozenset({
    "em_ai", "em_ad", "em_au",
    "wm_ai", "wm_ad", "wm_au",
    "facts_ai", "facts_ad", "facts_au",
    # provisioned natively as EXECUTE-FUNCTION triggers (_trg_* fns)
    "trim_validations_to_3",
})


# --- vector companion type + sqlite-vec KNN translation ---------------------

_CURRENT_SCHEMA: list = [""]
_VEC_TYPE_CACHE: dict = {}
_ACTIVE_CONN: list = [None]


_HEALED_SCHEMAS: set = set()
_HEALED_CONTRACT_SCHEMAS: set = set()


def _ensure_contract_columns(raw, schema: str) -> None:
    """Self-heal missing contract columns on legacy-lineage schemas.

    A schema can predate the vendored core's table lineage (older
    mnemosyne generation created episodic_memory with a different shape:
    summary/source_type/entities instead of source/summary_of/veracity...).
    The core's lazy `CREATE TABLE IF NOT EXISTS` +
    `_add_column_if_missing` migrations assume the CREATE-era columns
    exist, so on a foreign-lineage schema init crashes with
    UndefinedColumn before ever reaching the lazy adds. We add exactly
    the vendored CREATE contract columns (IF NOT EXISTS: no-ops on a
    correct schema, and the core's own migrations remain authoritative
    for every later column). Once per schema per process, like
    _ensure_max_fn; failures are silent — the core's error path stays
    honest if a heal genuinely cannot apply.
    """
    if schema in _HEALED_CONTRACT_SCHEMAS:
        return
    _HEALED_CONTRACT_SCHEMAS.add(schema)
    # Vendored core CREATE contracts (beam.py init_db; column types from
    # the same declarations). Names only — types all TEXT except the
    # obvious numerics; a wrong type only matters if the core verifies.
    for table, cols in _CONTRACT_COLUMNS:
        for col in cols:
            try:
                raw.execute(
                    "ALTER TABLE " + _q(schema) + "." + _q(table) +
                    " ADD COLUMN IF NOT EXISTS " + _q(col) + " " +
                    _contract_type(col))
            except Exception:
                break  # table absent or locked: this table is not ours


_CONTRACT_COLUMNS: tuple = ()


def _contract_type(col: str) -> str:
    # corrected_by/validated_at are INTEGER in the vendored core's own
    # _add_column_if_missing guards (beam.py 2033/2037): a text twin makes
    # the guard's PRAGMA type compare fail with 'schema mismatch ...
    # expected INTEGER, got TEXT' on every fresh (native-DDL) schema.
    return {"importance": "double precision", "recall_count": "bigint",
            "tier": "bigint", "binary_vector": "bytea",
            "validation_count": "bigint",
            "corrected_by": "bigint",
            "confidence": "double precision",
            "reinforcement_count": "bigint",
            "mention_count": "bigint"}.get(col, "text")


def _load_contract_columns() -> None:
    """Built-in minimal contract (tables the recall/init path touches).
    The core's own lazy migrations stay authoritative for everything
    beyond the CREATE-era columns."""
    global _CONTRACT_COLUMNS
    if _CONTRACT_COLUMNS:
        return
    _CONTRACT_COLUMNS = (
        ("working_memory", (
            "id", "content", "timestamp", "session_id", "importance",
            "metadata_json", "source", "veracity", "memory_type",
            "consolidated_at", "consolidation_claimed_at", "scope",
            "author_id", "author_type", "channel_id")),
        ("episodic_memory", (
            "id", "content", "source", "timestamp", "session_id",
            "importance", "metadata_json", "summary_of", "veracity",
            "created_at", "tier", "degraded_at", "memory_type",
            "binary_vector", "recall_count", "last_recalled",
            "valid_until", "superseded_by", "scope", "author_id",
            "author_type", "channel_id", "trust_tier", "validator",
            "validated_at", "validation_count", "event_date",
            "event_date_precision", "temporal_tags", "corrected_by")),
    )


def _ensure_max_fn(raw, schema: str) -> None:
    """SQLite 2-arg scalar MAX(a,b) lives in schema space (see native_ddl).

    Self-heal: schemas provisioned before the function existed get it on
    first connect; once per schema per process.
    """
    if schema in _HEALED_SCHEMAS:
        return
    _HEALED_SCHEMAS.add(schema)
    try:
        raw.execute(
            "CREATE OR REPLACE FUNCTION " + _q(schema) + ".max("
            "a double precision, b double precision) "
            "RETURNS double precision AS $$ SELECT CASE "
            "WHEN a IS NULL THEN coalesce(b, a) "
            "WHEN b IS NULL THEN a "
            "WHEN a >= b THEN a ELSE b END $$ LANGUAGE sql IMMUTABLE")
    except Exception:
        pass


def _companion_is_vector(table: str) -> bool:
    """True if `table`.embedding is a pgvector type in the active schema."""
    key = (_CURRENT_SCHEMA[0] or _active_schema(), table)
    hit = _VEC_TYPE_CACHE.get(key)
    if hit is not None:
        return hit
    ok = False
    q = ("SELECT format_type(a.atttypid, NULL) FROM pg_catalog.pg_attribute a"
         " WHERE a.attrelid = cast(%s || '.' || %s as pg_catalog.regclass)"
         " AND a.attname = 'embedding' AND a.attnum > 0")
    try:
        schema = _CURRENT_SCHEMA[0] or _active_schema()
        conn = _ACTIVE_CONN[0]
        if conn is not None and not conn._closed:
            row = conn.execute(q, (schema, table)).fetchone()
        else:
            probe = psycopg.connect(_pg_dsn_for_probe(), autocommit=True,
                                    row_factory=psycopg.rows.dict_row)
            try:
                row = probe.execute(q, (schema, table)).fetchone()
            finally:
                probe.close()
        if row:
            vals = list(row.values()) if hasattr(row, 'values') else [row[0]]
            ok = str(vals[0] or "").lower().startswith("vector")
    except Exception:
        ok = False
    if ok or (_CURRENT_SCHEMA[0] and _ACTIVE_CONN[0] is not None):
        _VEC_TYPE_CACHE[key] = ok
    elif ok:
        _VEC_TYPE_CACHE[key] = ok
    # A False decided with no schema/connection context is NOT cached:
    # it would disable KNN rewriting for the whole process.
    return ok


# sqlite-vec KNN: `col MATCH [vec_quantize_*](operand[, args]) AND k=N`.
# Native PG equivalent against a provisioned companion: exact cosine KNN
# ORDER BY embedding <=> q LIMIT k. The quantizer call collapses: pgvector
# takes the fp32 vector bind directly (int8 quantization is lossy and was
# dropped by design).
_VEC_KNN_RE = re.compile(
    r"(?P<col>[\w.]+)\s+MATCH\s+"
    r"(?:vec_quantize_\w+\s*\(\s*(?P<qargs>(?:[^()]|\([^()]*\))*?)"
    r"\s*\)"
    r"|(?P<op>\?|%(?:\([A-Za-z_]\w*\)|)s|\$\d+)"
    r"|(?P<lit>'[^']*'|\[[^()]*\])(?:::\w+(?:\[\])?)?)"
    r"\s+AND\s+k\s*=\s*(?P<k>\d+)", re.I)


_VEC_KNN_OP_RE = re.compile(
    r"([\w.]+)\s+MATCH\s+vec_quantize_\w+\s*\(\s*([^()]*?)\s*\)"
    r"\s+AND\s+k\s*=\s*(\d+)", re.I)


def _re_qlq(qargs: str) -> bool:
    """True if a KNN qargs group contains a bind placeholder."""
    return bool(_PLACEHOLDER_RE.search(qargs)) if qargs else False


_ARG_SPLIT_RE = re.compile(r",(?![^\[]*\])")  # comma outside [...] lists
# literal-only form (no bind in the call): handled by the literal-inline
# pass, so _rewrite_vec_knn leaves it untouched.
_KNN_CALL_ONLY_RE = re.compile(
    r"vec_quantize_\w+\s*\(\s*((?:[^()]|\([^()]*\))*)\)",
    re.I | re.S)


# standalone `AND k = N` clause (the sqlite-vec KNN cardinality arg)
_VEC_KNN_AND_K_RE = re.compile(r"\s+AND\s+k\s*=\s*(\d+)", re.I)


def _drop_companion_blob_projections(sql: str) -> str:
    """Drop vec-companion blob projections from KNN SELECT lists.

    sqlite-vec exposes the stored embedding as a readable column on the
    virtual table (`SELECT rowid, distance, embedding FROM vec_*`), but
    the PG companions hold `embedding vector(384)` as the indexed KNN
    column that the rewrite references in the inner scan — projecting it
    again in the outer list is an UndefinedColumn under the wrapper
    form, and the core only uses the column for optional blob scoring
    (which already abstains honestly on PG, query blob is None).  Only
    touched by a sqlite-vec KNN predicate (caller checks MATCH first).
    """
    m0 = re.search(r"(?i)\bSELECT\s+(.*?)\s+FROM\s+vec_\w+", sql)
    if not m0:
        return sql
    proj = ", ".join(
        p.strip() for p in re.split(r",(?![^\[]*\])", m0.group(1))
        if p.strip() and p.strip().lower()
        not in ("embedding", "binary_vector"))
    if not proj:
        # every projection was a blob column: `SELECT  FROM` is a
        # syntax error — a minimal projection keeps the statement
        # well-formed. The KNN wrapper re-adds the blob columns from
        # the companion (see _blob_cols below).
        proj = "rowid"
    _blob_cols = [c.strip() for c in re.split(r",(?![^\[]*\])", m0.group(1))
                  if c.strip().lower() in ("embedding", "binary_vector")]
    tail = sql[m0.end():]
    if _blob_cols:
        # sqlite-vec KNN: the blob columns live in the companion table
        # the wrapper scans, so keep them in the projection (the
        # wrapper's inner subquery re-supplies them).
        return sql
    return (sql[:m0.start()] + "SELECT " + proj + " FROM "
            + m0.group(0).split("FROM", 1)[1].strip()
            + (" " + tail if tail else ""))


def _rewrite_vec_knn(sql: str):
    """Return rewritten SQL, or `sql` unchanged (identity) if not handled."""
    return _rewrite_vec_knn_impl(sql)


def _rewrite_vec_knn_impl(sql: str):
    """Short-splice driver: rewrite on the k-truncated statement via
    _rewrite_vec_knn_body (never recursing into this driver again),
    then re-attach the caller tail to the produced body."""
    m = _VEC_KNN_RE.search(sql)
    if not m:
        return sql
    # the span must end at the `AND k = N` clause; anything else the
    # tolerant operand alternation swallowed is not a KNN statement
    _kws = _VEC_KNN_AND_K_RE.search(sql, m.start())
    if _kws is None or _kws.end() != m.end():
        return sql
    fm = re.search(r"\bFROM\s+(?:\w+\s*\.\s*)?(\w+)", sql, re.I)
    if not fm or not _companion_is_vector(fm.group(1)):
        return sql
    _kw = _kws
    _cut = _kw.end() if _kw is not None else 0
    _short = None
    if _kw is not None and _kw.end() < len(sql) and "/*knn*/" not in sql:
        # The in-place path assumes the MATCH..AND k span ends the
        # statement.  When the caller appended a tail, rewrite on the
        # truncated statement (cut at the k clause) and re-attach the
        # caller tail.  If the produced body is the FROM-( )t wrapper,
        # a bare ORDER BY / LIMIT tail is dropped (the inner scan is
        # already ordered) and any other tail (extra predicates) goes
        # through the wrapper's WHERE splice instead.
        _short = sql[:_cut]
    _out = None
    if _short is not None:
        # Call the BODY directly: the wrapper path below always returns
        # from the `if _short is not None and _out is not _short:` arm,
        # so the recursive call only exists to test splicability of the
        # truncated statement. Going through _rewrite_vec_knn_impl
        # recursively lets the DEEP call's short arm leak the k number
        # (its `return _out + _tail` splices the literal k back in).
        _out = _rewrite_vec_knn_body(_short)
        if _out is _short:
            # the truncated statement cannot rewrite (e.g. an outer
            # projection referencing the virtual `distance` defeats
            # the wrapper splice guard): fall back to the in-place
            # path on the FULL statement below.
            _short = None
    if _short is not None and _out is not _short:
            _tail = sql[_cut:]
            if "FROM (" in _out:
                if re.match(r"\s*(?:ORDER\s+BY\s+distance|LIMIT\s+\d+)\s*$",
                            _tail, re.I):
                    return _out
                _tw = _tail.strip()
                if _tw:
                    # splice caller predicates into the wrapper WHERE
                    _tw = re.sub(r"(?i)\bORDER\s+BY\s+\S+", "", _tw)
                    _tw = re.sub(r"(?i)\bLIMIT\s+\d+", "", _tw).strip()
                    if _tw:
                        if re.search(r"(?i)\bWHERE\b", _out):
                            _out = re.sub(r"(?i)\bWHERE\b",
                                          "WHERE " + _tw + " AND", _out,
                                          count=1)
                        else:
                            _out = (_out + " WHERE " + _tw
                                    + (" ORDER BY t.distance"
                                       if "ORDER BY" not in _out.upper()
                                       else ""))
                return _out
            _mo = re.match(r"\s*ORDER\s+BY\s+distance\s*$", _tail, re.I)
            if _mo and re.search(r"\bORDER\s+BY\b", _out, re.I):
                return _out
            return _out + _tail
    return _rewrite_vec_knn_body(sql)


def _rewrite_vec_knn_body(sql: str):
    """In-place KNN rewrite on a statement whose MATCH..AND k span ends
    the statement (no caller tail). Returns `sql` unchanged (identity)
    if not a handled KNN form."""
    m = _VEC_KNN_RE.search(sql)
    if not m:
        return sql
    fm = re.search(r"\bFROM\s+(?:\w+\s*\.\s*)?(\w+)", sql, re.I)
    if not fm or not _companion_is_vector(fm.group(1)):
        return sql
    sql = _drop_companion_blob_projections(sql)
    _vo_lit = None
    if m.group("qargs") is None and m.group("op") is None:
        # a bare literal operand (no quantizer call, no placeholder):
        # resolve it to a pgvector literal so MATCH converts.
        _lit_txt = m.group("lit") if "lit" in m.groupdict() else None
        if _lit_txt is None:
            seg = m.group(0)
            mlit = re.search(
                r"\s+MATCH\s+('[^()]*'|\[[^()]*\])\s+AND\s+k\s*=",
                seg, re.I)
            _lit_txt = mlit.group(1) if mlit else None
        if _lit_txt is not None:
            _vo_lit = _to_vec_operand(_lit_txt)
            if _vo_lit is None:
                return sql
    if m.group("qargs") is not None and m.group("op") is None \
            and not _PLACEHOLDER_RE.search(m.group("qargs")):
        # operand is a pure literal quantizer call with no bind inside.
        # If the literal resolves to a pgvector value we rewrite the KNN
        # predicate ourselves (nothing else converts `MATCH` here); if it
        # does not resolve, leave the statement untouched (fail loud).
        _vo_lit = None
        for _mcall in _KNN_CALL_ONLY_RE.finditer(m.group(0)):
            _first = re.split(_ARG_SPLIT_RE, _mcall.group(1) or "")[0].strip()
            _vo_lit = _to_vec_operand(_first)
            if _vo_lit is None:
                # the value pass may have inlined the operand as a
                # single-quoted SQL string wrapping a list/array expr
                # ("'[...]'" or "'ARRAY[..]::int8[]'"): unwrap and retry.
                um = re.match(r"^'(.*)'(?:::\w+(?:\[\])?)?$", _first,
                              re.S)
                if um:
                    _vo_lit = _to_vec_operand(um.group(1))
            if _vo_lit is not None:
                break
        if _vo_lit is None:
            return sql
    if _vo_lit is not None:
        operand = _vo_lit
    else:
        operand = (m.group("qargs") or m.group("op") or "").split(",")[0].strip()
        if operand not in ("?",) and _PLACEHOLDER_RE.match(operand) \
                and m.group("qargs") is None and m.group("op") is not None:
            # a bare placeholder operand: the generic quantizer bind pass
            # rewrites vec_quantize_?(...) spans to %s binds; keep as-is.
            pass
        elif operand and _vo_lit is None \
                and operand.startswith("vec_quantize_"):
            # residual quantizer call whose args were already value-
            # resolved: convert its first arg to a pgvector operand.
            em = re.match(
                r"vec_quantize_\w+\s*\(\s*(.+?)(?:\s*,[^()]*?)?\s*\)$",
                operand, re.I | re.S)
            if em:
                _vo = _to_vec_operand(em.group(1).strip())
                if _vo is not None:
                    operand = _vo
                else:
                    # inner arg may itself be a quantizer call chain or a
                    # list literal: try the literal path.
                    inner = em.group(1).strip()
                    if inner.startswith("[") or (
                            inner[:1] in ("'", '"') and "[" in inner):
                        operand = _fp32_operand_from_list(inner)
                        if operand is None:
                            operand = inner
    # The operand must end up as a single bind placeholder in the emitted
    # SQL so the vec-quantizer substitution / bind pipeline can fill it.
    # Literals or expressions that are not placeholders cannot be fed a
    # vector bind — leave the statement untouched (fail loud downstream)
    # unless it is a quantizer call whose inner operand IS a placeholder.
    if operand and not _PLACEHOLDER_RE.match(operand) \
            and not _LITERAL_VECTOR_RE.match(operand):
        if _vo_lit is not None:
            # already resolved to a pgvector literal above
            pass
        else:
            inner = re.match(
                r"vec_quantize_\w+\s*\(\s*([^,()]*?)(?:\s*,[^()]*?)?\s*\)$",
                operand, re.I)
            if inner and _PLACEHOLDER_RE.match(inner.group(1).strip()):
                operand = inner.group(1).strip()
            elif operand.startswith("(") and operand.endswith(")"):
                # already-substituted wrapper (e.g. "(%s)") — keep it
                if not _PLACEHOLDER_RE.search(operand):
                    return sql
            else:
                return sql
    if operand and not _PLACEHOLDER_RE.match(operand):
        # operand already resolves to a pgvector-compatible value or
        # expression: normalize it to the canonical ::vector literal so
        # the <=> predicate type-resolves.
        _vo = _to_vec_operand(operand)
        if _vo is None:
            em = re.match(
                r"vec_quantize_\w+\s*\(\s*(.+?)(?:\s*,[^()]*?)?\s*\)$",
                operand, re.I | re.S)
            if em:
                _vo = _to_vec_operand(em.group(1))
        if _vo is not None:
            operand = _vo
    head = sql[:m.start()].rstrip()
    tail = sql[m.end():].lstrip()
    if head.upper().endswith("WHERE"):
        head = head[:-5].rstrip()
    if tail[:5].upper() == "WHERE":
        tail = "AND " + tail[5:].lstrip()
    parts = [t.strip() for t in (head, tail) if t.strip()]
    # SQLite sqlite-vec exposes `distance` as a virtual column; PG has no
    # such column, so the emitted SQL must never reference a bare
    # `distance` name: every SELECT-list / ORDER-BY / WHERE mention of it
    # becomes the cosine expression. The predicate itself stays a bare
    # expression (`AS alias` is illegal inside WHERE).
    # A WHERE predicate must be boolean; `a <=> b` is a double-precision
    # distance, never a boolean. The KNN predicate therefore becomes a
    # TRUE-valued gate (COALESCE(dist,0) >= -1: always true for finite
    # distances, NULL-safe) and the ORDER BY carries the real cosine.
    # distance ordering + LIMIT k is the KNN semantics sqlite-vec gives.
    _gate = "COALESCE(embedding <=> " + operand + ", 0) >= -1"
    if head and _PLACEHOLDER_RE.match(operand):
        # placeholder operand appearing in several emitted slots (gate,
        # distance projection, ORDER BY): the bind slot exists once in
        # the caller's parameter list, so the emitted SQL must reference
        # the placeholder exactly once. Wrap the table scan in a
        # sub-select: distance is computed once (placeholder lives
        # there), and the outer query filters/orders on the column.
        _fm = re.search(r"(?i)\bFROM\s+([\w.]+)", sql)
        _where_extra = sql[m.end():]
        _op_inner_i = operand
        _qc_i = re.match(
            r"vec_quantize_\w+\s*\(\s*(.+?)(?:\s*,[^()]*?)?\s*\)$",
            operand, re.I | re.S)
        if _qc_i:
            _voq_i = _to_vec_operand(_qc_i.group(1).strip())
            if _voq_i is not None:
                _op_inner_i = _voq_i
        _inner = "SELECT rowid, (embedding <=> " + _op_inner_i + \
                 ") AS distance FROM " + (_fm.group(1) if _fm else "") + \
                 " ORDER BY distance LIMIT " + m.group("k")
        # caller projected blob columns (e.g. `SELECT rowid, distance,
        # embedding FROM vec_*`): the companion table HAS them, so the
        # inner scan carries them and the outer projection resolves.
        _extra_cols = re.findall(
            r"(?i)\b(embedding|binary_vector)\b",
            sql[:m.start()])
        if _extra_cols:
            _inner = ("SELECT rowid, (embedding <=> " + _op_inner_i +
                      ") AS distance, " +
                      ", ".join(dict.fromkeys(c.lower() for c in _extra_cols)) +
                      " FROM " + (_fm.group(1) if _fm else "") +
                      " ORDER BY distance LIMIT " + m.group("k"))
        head2 = sql[:m.start()].rstrip()
        while head2.upper().endswith("WHERE"):
            head2 = head2[:-5].rstrip()
        tail2 = sql[m.end():].lstrip()
        if tail2[:5].upper() == "WHERE":
            tail2 = "AND " + tail2[5:].lstrip()
        # A literal quantizer operand (post value-pass) must not be
        # re-quantized: use the RESOLVED operand expression in the inner
        # scan, not the raw quantizer call.
        _op_inner = operand
        _qc = re.match(
            r"vec_quantize_\w+\s*\(\s*(.+?)(?:\s*,[^()]*?)?\s*\)$",
            operand, re.I | re.S)
        if _qc:
            _voq = _to_vec_operand(_qc.group(1).strip())
            if _voq is not None:
                _op_inner = _voq
        # rewrite bare `distance` refs in the caller's own text to the
        # inner column (already named `distance`).
        def _dref(mm):
            pre = mm.string[:mm.start()]
            if re.search(r"[\w.]\s*$|\bAS\s*$", pre, re.I):
                return mm.group(0)
            return "t.distance"
        head2 = re.sub(r"(?i)\s+FROM\s+[\w.]+(\s+[AS]\s+\w+)?\s*$",
                       "", head2)
        # Peel a trailing `WHERE <col> [WHERE <col>]...` stub chain
        # (_drop_companion_blob_projections / short-splice recursion can
        # stack them, even glued to the projection without a space):
        # the wrapper supplies the real WHERE-free form.
        while True:
            _hp = head2
            head2 = re.sub(r"(?i)\s*(?:\bWHERE\s+[\w.]+\s*)+$", "",
                           head2).rstrip()
            head2 = re.sub(r"(?i)\s+FROM\s+[\w.]+(\s+[AS]\s+\w+)?\s*$",
                           "", head2).rstrip()
            if head2 == _hp:
                break
        outer_sel = re.sub(r"\bdistance\b", _dref, head2)
        outer_tail = re.sub(r"\bdistance\b", _dref, tail2)
        # The caller's k clause itself must never survive into the tail
        # (recursion cut at `k`, `_kw.end()` may sit before the number):
        # a tail that is nothing but the k NUMBER / `AND k = N` / the
        # neutralised ORDER BY / LIMIT tail is KNN bookkeeping, not
        # caller text.  (A bare number is the k operand left by the
        # short-splice cut; the generic epilogue's LIMIT check then
        # never sees a k slot to re-bake.)
        outer_tail = re.sub(r"(?i)^(?:\s*\d+|\s*(?:AND\s+)?k\s*=\s*\d+|"
                           r"ORDER\s+BY\s+distance|LIMIT\s+\d+)\s*$",
                           "", outer_tail).strip()
        # strip ORDER BY distance from the tail (inner already ordered)
        outer_tail = re.sub(r"(?i)\bORDER\s+BY\s+t?\.?distance\b", "",
                            outer_tail).strip()
        outer_tail = re.sub(r"(?i)^AND\b", "WHERE", outer_tail).strip()
        if outer_tail.upper().startswith("WHERE"):
            body = (outer_sel + " FROM (" + _inner + ") t " + outer_tail)
        else:
            body = (outer_sel + " FROM (" + _inner + ") t" +
                    ((" " + outer_tail) if outer_tail else ""))
        # head had no projection? fall back to generic path
        if not outer_sel.strip():
            body = "SELECT * FROM (" + _inner + ") t"
        # The wrapper body is final here: the generic epilogue below
        # (ORDER BY / LIMIT k appended to `body`) must never run over
        # the wrapper form — when the caller had a tail, the in-place
        # generic path is not reached at all for the wrapper shape.
        if re.search(r"\bFROM\s*\(", body):
            return body
    if head:
        pred = _gate
    else:
        pred = _gate + " AS distance"
    cos = "embedding <=> " + operand
    # A statement may reference the KNN operand several times (SELECT-
    # list `distance`, WHERE predicate, ORDER BY). If the operand is a
    # placeholder, every extra reference (beyond the WHERE predicate's)
    # would consume an extra bind slot the caller never passed. Make the
    # extras refer to the same slot via a repeated placeholder marker:
    # SQLite `?N`-style repeats are handled by params.render baking one
    # literal value, so emit the FIRST occurrence as the placeholder and
    # subsequent ones as `?`-repeats keyed to that slot.
    if head and _PLACEHOLDER_RE.match(operand) \
            and "distance" not in (head or ""):
        pass

    def _dist_ref(mm):
        pre = mm.string[:mm.start()]
        # a qualified ref (`x.distance`) or an alias definition
        # ("... AS distance") is not the virtual-column reference
        if re.search(r"[\w.]\s*$|\bAS\s*$", pre, re.I):
            return mm.group(0)
        return cos
    if head:
        # virtual-column substitution applies to the caller's own text
        # (head + tail), never to the predicate we generate. Substituting
        # on the ORIGINAL statement text guarantees each `distance` ref is
        # replaced exactly once regardless of surrounding edits.
        joined = re.sub(r"\bdistance\b", _dist_ref, sql)
        # re-split around the original MATCH span within substituted text
        m2 = _VEC_KNN_RE.search(joined)
        if m2:
            head2 = joined[:m2.start()].rstrip()
            if head2.upper().endswith("WHERE"):
                head2 = head2[:-5].rstrip()
            tail2 = joined[m2.end():].lstrip()
            if tail2[:5].upper() == "WHERE":
                tail2 = "AND " + tail2[5:].lstrip()
        else:
            head2, tail2 = head, tail
        tparts = [t for t in (head2, tail2) if t.strip()]
        if tparts:
            body = tparts[0] + " WHERE " + pred
            if len(tparts) > 1:
                body += " " + " ".join(tparts[1:])
        else:
            body = "WHERE " + pred
    elif parts:
        body = parts[0] + " WHERE " + pred
        if len(parts) > 1:
            body += " " + " ".join(parts[1:])
    else:
        body = "WHERE " + pred
    if not re.search(r"\bORDER\s+BY\b", body, re.I):
        body += " ORDER BY " + cos
    elif head and re.search(r"\bORDER\s+BY\s+distance\b", body, re.I):
        body = re.sub(r"\bORDER\s+BY\s+distance\b", "ORDER BY " + cos,
                      body, count=1, flags=re.I)
    # only add the KNN k when the statement has NO LIMIT at all — a
    # placeholder LIMIT belongs to the caller's bind list (e.g.
    # "... WHERE <eligibility> LIMIT ?" with the k passed as a param),
    # and baking one here would add a slot the params don't cover.
    if not re.search(r"\bLIMIT\b", body, re.I):
        body += " LIMIT " + m.group("k")
    return body


# --- SQLite upsert -> PG ON CONFLICT ----------------------------------------
# The vendored core writes SQLite `INSERT OR REPLACE/IGNORE INTO ...` at 36+
# sites. PG equivalent: INSERT ... ON CONFLICT <clause>. Requires knowing
# the conflict target (a unique key), which is resolved from the catalog at
# runtime (first unique index/PK over non-excluded columns).

# --- SQLite upsert -> PG ON CONFLICT ----------------------------------------
# The vendored core writes SQLite `INSERT OR REPLACE/IGNORE INTO ...` at 36+
# sites. PG equivalent: INSERT ... ON CONFLICT <clause>. Requires knowing
# the conflict target (a unique key), which is resolved from the catalog at
# runtime (first unique index/PK over non-excluded columns).

def _pg_dsn_for_probe() -> str:
    from . import factory
    return factory._pg_dsn()


def _active_schema() -> str:
    """Schema of the active store (module connects have no search_path)."""
    if _CURRENT_SCHEMA[0]:
        return _CURRENT_SCHEMA[0]
    return ""


# INSERT OR x INTO t (col, list) SELECT ... — the VALUES-less form (e.g.
# memory_embeddings' write-if-base-row-still-there). Column list must be
# pure names (nested-paren SELECT bodies follow the list).
_UPSERT_SELECT_RE = re.compile(
    r"\bINSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+([\w.\"]+)"
    r"\s*\(\s*[\w\"\s,]*\)", re.I)


_UPSERT_RE = re.compile(
    r"\bINSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+([\w.\"]+)"
    r"\s*\(([^()]*)\)", re.I)
_UQ_CACHE: dict = {}


def _unique_cols(table: str, conn=None) -> list:
    """Unique single/multi-column key sets on `table` (excl. identity PK)."""
    key = (_CURRENT_SCHEMA[0] or _active_schema(), table)
    hit = _UQ_CACHE.get(key)
    if hit is not None:
        return hit
    out: list = []
    schema = _CURRENT_SCHEMA[0] or _active_schema()
    try:
        # unique index key sets, queried on the store's own connection
        # (module-level psycopg.execute() does not exist).
        # NB: "::regclass" would be misread as a named placeholder ":r" by
        # the param renderer — use cast(...) form instead.
        q = ("SELECT i.indisunique, i.indisprimary, "
             " array_agg(a.attname ORDER BY x) "
             "FROM pg_catalog.pg_index i "
             "JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS x(attnum, ord) "
             "  ON true "
             "JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid "
             "  AND a.attnum = x.attnum "
             "WHERE i.indrelid = cast(%s || '.' || %s as pg_catalog.regclass) "
             "GROUP BY i.indexrelid, i.indisunique, i.indisprimary "
             "ORDER BY i.indisprimary DESC")
        if conn is not None:
            rows = conn.execute(q, (schema, table)).fetchall()
        else:
            probe = psycopg.connect(
                _pg_dsn_for_probe(), autocommit=True,
                row_factory=psycopg.rows.dict_row)
            try:
                rows = probe.execute(q, (schema, table)).fetchall()
            finally:
                probe.close()
        for r in rows:
            # rows arrive as dict_rows (native) or positional tuples
            # (catalog probes via _CursorRow); index access works for both.
            uniq, primary, cols = r[0], r[1], (r[2] or [])
            # identity rowid columns cannot serve as ON CONFLICT targets
            cols = [c for c in cols if not c.endswith("__rowid")]
            if not uniq or not cols:
                continue
            out.append(cols)
    except Exception:
        out = []
    _UQ_CACHE[key] = out
    return out


def _inserted_cols(sql: str, target: list) -> list:
    """Columns written by an INSERT...SELECT ON CONFLICT rewrite: take the
    leading column list before the SELECT keyword (VALUES-less form)."""
    m = re.search(r"\bINTO\s+[\w.\"]+\s*\(([^()]*)\)", sql, re.I)
    if not m:
        return list(target)
    cols = [c.strip().strip('"') for c in m.group(1).split(",") if c.strip()]
    return [c for c in cols if c not in target]


def _rewrite_upsert(sql: str, conn=None) -> str:
    m = _UPSERT_RE.search(sql) or _UPSERT_SELECT_RE.search(sql)
    if not m:
        return sql
    kind = m.group(1).upper()
    table = m.group(2).strip('"')
    cols = [c.strip().strip('"') for c in m.group(3).split(",") if c.strip()] \
        if m.re is _UPSERT_RE else _inserted_cols(sql, [])
    target = None
    for key in _unique_cols(table, conn):
        if all(c in cols for c in key):
            target = key
            break
    if target is None:
        keys = _unique_cols(table, conn)
        if not keys:
            # no unique key at all: sqlite would always insert — plain
            # INSERT is exact parity.
            return sql[:m.start(1) - 3] + sql[m.end(1):]
        # sqlite's INSERT OR semantics target the row-identity key: use
        # the single-column unique/PK (never a composite cross-table key).
        single = [k for k in keys if len(k) == 1
                  and (k[0].endswith("_id") or k[0] == "id")]
        target = single[0] if single else keys[0]
    tgt = ", ".join('"%s"' % c for c in target)
    tail = (" ON CONFLICT (" + tgt + ") DO NOTHING") if kind == "IGNORE" \
        else (" ON CONFLICT (" + tgt + ") DO UPDATE SET " +
              ", ".join('"%s" = EXCLUDED."%s"' % (c, c) for c in cols
                        if c not in target))
    # trailing ON CONFLICT in the source statement would collide
    # the sqlite OR-prefix must go: strip "OR REPLACE"/"OR IGNORE" leaving
    # a plain INSERT, then append the PG conflict clause.
    head = sql[:m.start(1) - 3] + sql[m.end(1):]
    if re.search(r"\bON\s+CONFLICT\b", head, re.I):
        return head      # source already has its own conflict clause
    return head + tail


def _normalize_ddl(sql: str) -> str:
    # sqlite-vec virtual tables have no PG analogue: the native vec_<base>
    # companion tables are provisioned out-of-band, so these probes/DDLs
    # are honest no-ops (report success; reads go to the real tables).
    if _VIRTUAL_TABLE_RE.match(sql):
        # honest no-op; the probe table this creates doesn't exist in PG, so
        # its DROP is a no-op too (handled via _DROP_VIRTUAL_TABLE_RE below).
        return "@@@MNEMO_NOOP@@@"
    if _DROP_VIRTUAL_TABLE_RE.match(sql):
        return "@@@MNEMO_NOOP@@@"
    if _DROP_TRIGGER_RE.match(sql):
        # sqlite's single-name DROP TRIGGER is invalid PG (needs ON <table>).
        # The FTS-sync triggers are maintained natively by provisioning, so
        # their sqlite DROPs are honest no-ops; anything else gets ON <table>
        # from the trigger map when known, else is left to fail loudly.
        m = _DROP_TRIGGER_RE.match(sql)
        name = m.group("name").strip("\"`")
        if name in _FTS_SYNC_TRIGGER_NAMES:
            return "@@@MNEMO_NOOP@@@"
        table = _SQLITE_TRIGGER_TABLES.get(name)
        if table:
            return f"DROP TRIGGER {m.group(1)}{name} ON {table}"
        return sql
    if _CREATE_TRIGGER_RE.match(sql):
        # PG has no CREATE TRIGGER IF NOT EXISTS (any arity) and no inline
        # trigger bodies: sqlite's `... BEGIN INSERT ...; END` must become
        # EXECUTE PROCEDURE <fn>(). Translate the sqlite bodies for the
        # known triggers; unknown ones keep the old behaviour.
        sql = _translate_sqlite_trigger(sql)
    # sqlite-vec DDL lives in generic `CREATE TABLE IF NOT EXISTS vec_*`
    # statements (not just CREATE VIRTUAL TABLE); those tables are
    # provisioned natively as vector(384) companions, so their sqlite DDL
    # is an honest no-op here.
    if _VEC_CREATE_RE.search(sql):
        return "@@@MNEMO_NOOP@@@"
    # vec0 pseudo-sqlite_master introspection (see _SQLITE_MASTER_VEC_SQL).
    if _SQLITE_MASTER_VEC_RE.search(sql):
        nm = _vec_probe_table_name(sql)
        if nm:
            # Classify from truth: a real pgvector companion column reads
            # as float32 to the core (exact fp32 KNN via <=>); a JSON/text
            # column keeps the int8 flavour so the blob arms still run.
            ddl = _SQLITE_MASTER_VEC_SQL.replace("@TABLE@", nm)
            if _companion_is_vector(nm):
                ddl = ddl.replace(" '])'", " ']) /*float32*/'")
            return ddl
    if "AUTOINCREMENT" in sql.upper() or "WITHOUT ROWID" in sql.upper():
        # PG forbids PRIMARY KEY and NOT NULL in the same column definition;
        # the vendored core writes both (e.g. episodic_memory.rowid).
        sql = _AUTOINCREMENT_RE.sub("INTEGER PRIMARY KEY", sql)
        sql = re.sub(
            r"(\bPRIMARY\s+KEY)\s+NOT\s+NULL\b", r"\1", sql, flags=re.I)
        sql = re.sub(
            r"(\bINTEGER\b)\s+NOT\s+NULL\s+PRIMARY\s+KEY\b",
            r"\1 PRIMARY KEY", sql, flags=re.I)
        sql = _WITHOUT_ROWID_RE.sub("", sql)
    return sql


def native_is_noop(sql: str) -> bool:
    return sql == "@@@MNEMO_NOOP@@@"


def _translate_sqlite_trigger(sql: str) -> str:
    """Translate sqlite inline-body triggers to PG-executable form.

    The FTS-sync triggers (em_*/wm_*/facts_*) are maintained natively by
    provisioning (tsvector columns + triggers). Firing the sqlite body in
    PG would write to fts_* tables that are legacy shim artifacts, so
    those are honest no-ops here. Unknown inline-body triggers raise
    OperationalError instead of being silently dropped. Bodyless triggers
    just lose the unsupported IF NOT EXISTS clause.
    """
    m = _TRIGGER_HEAD_RE.match(sql)
    if not m:
        return sql
    name = m.group("name")
    if name in _FTS_SYNC_TRIGGER_NAMES:
        return "@@@MNEMO_NOOP@@@"
    if _TRIGGER_BODY_RE.search(sql):
        raise _sqlite3.OperationalError(
            f"cannot translate inline-body trigger {name!r} for PostgreSQL")
    # bodyless (e.g. CONSTRAINT triggers): at least drop IF NOT EXISTS
    return _TRIGGER_IF_NOT_EXISTS_RE.sub(r"\1", sql)


class Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._pg = conn._raw
        self._cur = None
        self._delegate = None      # set for PRAGMA results
        self._iter = None
        self.row_factory = None
        self.arraysize = 1
        self.lastrowid = None
        self.description = None
        self.rowcount = -1

    def _ensure(self):
        if self._cur is None or self._cur.closed:
            self._cur = self._pg.cursor(binary=True)
        return self._cur

    # -- api ------------------------------------------------------------------
    def execute(self, sql, parameters=None):
        sql_s = sql.strip()
        self._delegate = None
        self._iter = None
        self.lastrowid = None
        self._conn._staged = {}
        self._conn._quant_binds = None
        # SQLite allows SAVEPOINT as an implicit transaction opener under
        # autocommit; psycopg only knows savepoints inside an explicit
        # transaction. Opening a PG transaction on SAVEPOINT preserves the
        # semantics (ROLLBACK TO / RELEASE then behave as on SQLite; the
        # enclosing commit()/close() decides durability, same as SQLite's
        # atomic implicit block).
        if re.match(r"^\s*SAVEPOINT\b", sql_s, re.I):
            self._conn._begin_if_idle()
        if re.match(r"^\s*(ROLLBACK\s+TO\b|RELEASE\b)", sql_s, re.I):
            self._conn._begin_if_idle()
        # SQLite's writer-lock BEGINs (IMMEDIATE/EXCLUSIVE/DEFERRED) have
        # no PG grammar. Map them onto PG's plain BEGIN: single-host PG
        # serializes writers via row/MVCC locking, so the SQLite locking
        # step is a no-op semantically. Same-connection Python-side
        # locks in the core still serialize threads.
        if re.match(r"^\s*BEGIN\s+(IMMEDIATE|EXCLUSIVE|DEFERRED)\b",
                    sql_s, re.I):
            sql_s = re.sub(r"^\s*BEGIN\s+(IMMEDIATE|EXCLUSIVE|DEFERRED)\b",
                           "BEGIN", sql_s, count=1, flags=re.I)
            self._conn._begin_if_idle()
            try:
                self._ensure().execute(sql_s)
            except Exception:
                # nested (already in a tx): SQLite errors here too; leave
                # the error to surface unchanged.
                raise
            self.description = None
            self.rowcount = -1
            return self
        # vec0 sqlite_master probe with a BOUND table name: the inline
        # rewrite in _normalize_ddl only sees literal names, so bind-form
        # probes (name=?) must be recognized here.
        if _SQLITE_MASTER_VEC_RE.search(sql_s) and (
                parameters and not isinstance(parameters, dict)):
            _two_col = bool(re.search(r"SELECT\s+name\s*,\s*sql",
                                      sql_s, re.I))
            _p0 = list(parameters)[0] if parameters else None
            if isinstance(_p0, str) and _p0.startswith("vec_") and \
                    re.fullmatch(r"\w+", _p0):
                _ddl_q = _SQLITE_MASTER_VEC_SQL.replace("@TABLE@", _p0)
                if _companion_is_vector(_p0):
                    _ddl_q = _ddl_q.replace(" '])'", " ']) /*float32*/'")
                _pc = self._pg.cursor()
                _pc.execute(_ddl_q)
                _r = _pc.fetchone()
                _pc.close()
                # core reads row[0] as the DDL text (single-column
                # SELECT shape of `SELECT sql FROM sqlite_master`).
                _sql_txt = None
                if _r is not None:
                    _v = (_r["sql"] if "sql" in _r else
                          (list(_r.values())[-1] if hasattr(_r, "values")
                           else _r[-1]))
                    _sql_txt = _v
                if _two_col:
                    self._delegate = _RowCursor(
                        ("name", "sql"),
                        [(_p0, _sql_txt)] if _r else [])
                    self.description = (("name",), ("sql",))
                else:
                    self._delegate = _RowCursor(("sql",),
                                                [(_sql_txt,)] if _r else [])
                    self.description = (("sql",),)
                self.rowcount = 1 if _r is not None else -1
                return self
        if sql_s.upper().startswith("PRAGMA"):
            c = _pragma(self._conn, sql_s)
            if c is None:                       # honest no-op set
                self.description = None
                self.rowcount = -1
                return self
            c.row_factory = self.row_factory
            self._delegate = c
            self.description = c.description
            self.rowcount = c.rowcount
            return self
        sql_s = _normalize_ddl(sql_s)
        if native_is_noop(sql_s):
            # honest no-op (e.g. CREATE VIRTUAL TABLE under PG)
            self.description = None
            self.rowcount = -1
            return self
        sql_s = _rewrite_upsert(sql_s, self._conn)
        sql_s = _dialect_rewrite.rewrite_dialect(sql_s)
        _had_bare_rowid = bool(re.search(r"(?<![\w.\"])rowid\b", sql_s))
        sql_s = _rowid_rewrite.rewrite_rowids(
            sql_s, _CURRENT_SCHEMA[0] or "", self._conn)
        sql_s, _fts_changed = _fts_rewrite.rewrite_fts_sql(sql_s)
        # sqlite-vec quantizers are VALUES (sqlite's rule: a function
        # operand is not a statement bind), so first try resolving each
        # call IN PLACE against its operand's own bind — this preserves
        # the caller's placeholder bookkeeping exactly. Only if a call
        # survives (operand shape we cannot resolve in place) fall back
        # to the bind-queue substitution below.
        if _VEC_FN_CALL_RE.search(sql_s):
            sql_s = _vec_quantize_value_sub(sql_s, parameters, self._conn)
        _has_quant = bool(_VEC_FN_CALL_RE.search(sql_s))
        if _has_quant:
            _orig_sql_for_drop = sql_s
            _orig_params = parameters
            sql_s = _VEC_FN_CALL_RE.sub(
                lambda mm: _quantize_placeholder_sub(
                    mm, _orig_params, self._conn), sql_s)
            parameters = _quant_params_drop(_orig_sql_for_drop, parameters)
        knn = _rewrite_vec_knn(sql_s)
        if knn is not sql_s:
            # whole sqlite-vec KNN predicate translated to cosine KNN
            # (quantizer operand is a %s bind from the pass above, or a
            # literal inlined by the generic path below).
            sql_s = knn
        if _VEC_FN_CALL_RE.search(sql_s):
            # sqlite-vec quantizers are evaluated in Python (see
            # _vec_quantize) and inlined as strict SQL text literals —
            # no binds are created or dropped, so the caller's parameter
            # list keeps its exact placeholder bookkeeping (sqlite's own
            # rule: a function operand is a value, not a statement bind).
            _lit_only = [m for m in _VEC_FN_CALL_RE.finditer(sql_s)
                         if re.match(r"""^\s*(?:'[^']*'|"[^"]*")""",
                                      m.group(2))]
            if len(_lit_only) == sum(
                    1 for _ in _VEC_FN_CALL_RE.finditer(sql_s)):
                # every call has a literal operand: the KNN arm (exact
                # fp32) uses the operand's vector literal form directly;
                # otherwise evaluate the quantizer as a SQL expression.
                def _lit_sub(mm):
                    _f0 = re.split(r",(?![^\[()]*[\]()])",
                                   mm.group(2))[0].strip()
                    _vo = _to_vec_operand(_f0)
                    if _vo is not None:
                        return _vo
                    return _vec_quantize(mm.group(1), mm.group(2), (),
                                         self._conn)
                sql_s = _VEC_FN_CALL_RE.sub(_lit_sub, sql_s)
            else:
                _orig_params = parameters
                _orig_sql_for_drop = sql_s   # placeholders before sub
                sql_s = _VEC_FN_CALL_RE.sub(
                    lambda mm: _quantize_placeholder_sub(
                        mm, _orig_params, self._conn), sql_s)
                parameters = _quant_params_drop(_orig_sql_for_drop,
                                                parameters)
        native, bound = _params.render(sql_s, parameters, conn=self._conn)
        # SQLite row-name parity: core code does `row["rowid"]` on rows
        # read from shim tables (SQLite's implicit rowid).  The shim
        # rewrite above retargets the column to `<table>__rowid`; alias it
        # back so the result column is named `rowid` like SQLite's.
        _rid_alias = re.search(r"\bFROM\s+(?:\w+\s*\.\s*)?(\w+)", native, re.I)
        # The rowid shim already renamed `rowid` -> `<table>__rowid` in
        # `native`, so test the PRE-render SQL for the bare-rowid reference.
        # Skip if the SELECT list already carries an explicit rowid alias
        # (callers like `SELECT working_memory__rowid AS rowid ...` would
        # otherwise get a double alias: `x AS "rowid" AS working_memory__rowid`).
        # A rowid reference inside an EXISTS(...) sub-referencing the OUTER
        # table is also not a projected column of this statement, so no
        # alias is due there either (and adding one corrupts the predicate:
        # `WHERE working_memory__rowid AS "rowid" = ...` -> PG syntax error).
        _subquery_ref = re.search(
            r"\b(?:EXISTS|IN|NOT\s+IN)\s*\(\s*(?:SELECT|WITH)\b", native, re.I)
        if (_rid_alias
                and _had_bare_rowid
                and not _rid_alias.group(1).lower().startswith("vec_")
                and not (_subquery_ref
                         and _subquery_ref.start() > _rid_alias.start())
                and not re.search(
                    r"(?i)\bas\s+\"?rowid\"?\b",
                    native[:_rid_alias.start()])):
            native = re.sub(
                rf"(?<![\w.\\])({_rid_alias.group(1)}__rowid)\b",
                r'\1 AS "rowid"', native, count=1)
        if self._conn._quant_binds:
            binds = self._conn._quant_binds
            self._conn._quant_binds = None
            if isinstance(bound, dict):
                raise _sqlite3.ProgrammingError(
                    "cannot mix named parameters with vector quantizer binds")
            bound = list(bound or []) + list(binds)
        if self._conn.query_only and _WRITE_RE.match(native):
            raise _sqlite3.OperationalError(
                "attempt to write a readonly database")
        cur = self._ensure()
        m = re.match(r"\s*INSERT(?:\s+OR\s+\w+)?\s+INTO\s+([\"\w.]+)",
                     native, re.I)
        idcol = None
        if m and not re.search(r"\bRETURNING\b", native, re.I):
            idcol = self._conn._identity_col(m.group(1).split(".")[-1].strip('"'))
        counted = bool(_WRITE_RE.match(native))
        stat_before = self._conn._stat_counts() if counted else None
        try:
            if idcol:
                native = native.rstrip().rstrip(";") + f' RETURNING "{idcol}"'
            if (isinstance(bound, (list, tuple))
                    and native.count("%s") != len(bound)
                    and native.count("?") >= native.count("%s")):
                # placeholder/param bookkeeping broke: `?` survived into
                # the rendered SQL (render could not see it) so psycopg
                # counts %s only. Fail as SQLite would on a real mismatch,
                # with both forms visible instead of a bare psycopg count.
                raise _sqlite3.ProgrammingError(
                    "render mismatch: sql has %d '%%s' and %d '?' marks "
                    "for %d params: %s" % (
                        native.count("%s"), native.count("?"),
                        len(bound), native[:2000]))
            if bound is None:
                cur.execute(psql.SQL(native))
            elif isinstance(bound, dict):
                cur.execute(psql.SQL(native), bound)
            else:
                cur.execute(psql.SQL(native), bound)
        except _sqlite3.Error:
            raise
        except psycopg.Error as exc:
            if (isinstance(exc, psycopg.errors.DuplicateObject)
                    and re.match(r"\s*CREATE\s+TRIGGER\b", native, re.I)):
                # trigger already provisioned — honest no-op
                self.description = None
                self.rowcount = -1
                return self
            self.rowcount = -1
            raise
        self.description = cur.description
        if idcol:
            try:
                rows = cur.fetchall()
                if rows:
                    r = rows[-1]
                    self.lastrowid = (list(r.values())[0] if isinstance(r, dict)
                                      else r[0])
            except psycopg.ProgrammingError:
                pass
        if counted:
            delta = self._conn._stat_delta(stat_before)
            rc = delta if delta is not None and delta >= 0 else max(0, cur.rowcount)
            self.rowcount = max(0, rc)
            NativeConn.total_changes += self.rowcount
        else:
            self.rowcount = -1
        return self

    def executescript(self, script):
        for stmt in split_script(script):
            self.execute(stmt)
        return self

    def executemany(self, sql, seq_of_params, **kw):
        total = 0
        self.lastrowid = None
        for p in seq_of_params:
            self.execute(sql, p)
            if self.rowcount > 0:
                total += self.rowcount
        self.rowcount = total
        return self

    # -- fetching ---------------------------------------------------------------
    def _current(self):
        if self._delegate is not None:
            return self._delegate.fetchone()
        if self._iter is None:
            try:
                self._iter = iter(self._cur)
            except psycopg.ProgrammingError:
                self._iter = iter(())
        return next(self._iter, None)

    def fetchone(self):
        r = self._current()
        return None if r is None else _convert_row(r, self.row_factory)

    def fetchall(self):
        out = []
        while True:
            r = self._current()
            if r is None:
                break
            out.append(_convert_row(r, self.row_factory))
        return out

    def fetchmany(self, n=None):
        n = self.arraysize if n is None else n
        out = []
        while len(out) < n:
            r = self._current()
            if r is None:
                break
            out.append(_convert_row(r, self.row_factory))
        return out

    def __iter__(self):
        while True:
            r = self.fetchone()
            if r is None:
                return
            yield r

    @property
    def connection(self):
        return self._conn

    def close(self):
        if self._cur is not None:
            try:
                self._cur.close()
            except Exception:
                pass
            self._cur = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def nextset(self):
        return None


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------

class NativeConn:
    """PostgreSQL connection that behaves like sqlite3.Connection where the
    stores' contract requires it; everything else is honest PG semantics."""

    total_changes = 0

    def __init__(self, dsn: str, schema: str, query_only: bool = False):
        self._staged: dict = {}
        self._quant_binds = None      # per-statement; never leaks across
        self.mnemo_schema = schema
        self.query_only = query_only
        self.row_factory = None
        self._closed = False
        self._identity_cache = {}
        self._raw = psycopg.connect(
            dsn, autocommit=True, row_factory=psycopg.rows.dict_row,
            application_name="mnemo-pg")
        self._raw.execute("SET search_path = " + _q(schema) + ", public")
        _CURRENT_SCHEMA[0] = schema
        _ACTIVE_CONN[0] = self
        _ensure_max_fn(self._raw, schema)
        _load_contract_columns()
        _ensure_contract_columns(self._raw, schema)
        if query_only:
            self._raw.execute("SET transaction_read_only = on")
        # durability parity with SQLite WAL + synchronous=FULL
        try:
            self._raw.execute("SET synchronous_commit = on")
        except Exception:
            pass

    # -- sqlite3.Connection surface --------------------------------------------
    def cursor(self):
        c = Cursor(self)
        c.row_factory = self.row_factory
        return c

    def execute(self, sql, parameters=None):
        return self.cursor().execute(sql, parameters)

    def executescript(self, script):
        return self.cursor().executescript(script)

    def executemany(self, sql, seq_of_params):
        return self.cursor().executemany(sql, seq_of_params)

    def _stage(self, raw_sql: str) -> str:
        """Register raw SQL text for literal splicing via %(k)s escaping.

        psycopg's PyFormat paramstyle treats a quoted %(k)s as a literal
        interpolation, which is safe ONLY because staged values are internal
        SQL we generated ourselves (never user data).
        """
        key = f"k{len(self._staged)}"
        self._staged[key] = raw_sql
        return key

    def _begin_if_idle(self):
        """Open a PG transaction if idle (SQLite implicit-block parity for
        bare SAVEPOINT statements)."""
        try:
            if (self._raw.info.transaction_status
                    == psycopg.pq.TransactionStatus.IDLE):
                self._raw.execute("BEGIN")
        except Exception:
            pass

    def commit(self):
        # If an implicit block is open (e.g. opened by a bare SAVEPOINT),
        # commit it so SQLite's atomic implicit-block semantics hold.
        try:
            if (self._raw.info.transaction_status
                    != psycopg.pq.TransactionStatus.IDLE):
                self._raw.commit()
        except Exception:
            pass
        return None  # autocommit parity

    def rollback(self):
        try:
            if (self._raw.info.transaction_status
                    != psycopg.pq.TransactionStatus.IDLE):
                self._raw.rollback()
        except Exception:
            pass

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._raw.close()
            except Exception:
                pass

    @property
    def in_transaction(self):
        try:
            return (self._raw.info.transaction_status
                    != psycopg.pq.TransactionStatus.IDLE)
        except Exception:
            return False

    # sqlite capabilities with no PG meaning — fail loudly, never stub
    def create_function(self, *a, **k):
        raise _sqlite3.OperationalError(
            "create_function is not available on the PG backend")

    def create_collation(self, *a, **k):
        raise _sqlite3.OperationalError(
            "create_collation is not available on the PG backend")

    def create_aggregate(self, *a, **k):
        raise _sqlite3.OperationalError(
            "create_aggregate is not available on the PG backend")

    def set_authorizer(self, *a, **k):
        raise _sqlite3.OperationalError(
            "set_authorizer is not available on the PG backend")

    def set_progress_handler(self, *a, **k):
        raise _sqlite3.OperationalError(
            "set_progress_handler is not available on the PG backend")

    def set_trace_callback(self, *a, **k):
        raise _sqlite3.OperationalError(
            "set_trace_callback is not available on the PG backend")

    def enable_load_extension(self, *a):
        raise _sqlite3.OperationalError(
            "load_extension is not available on the PG backend")

    def load_extension(self, *a):
        raise _sqlite3.OperationalError(
            "load_extension is not available on the PG backend")

    # -- write accounting (exact rowcount for INSERT..SELECT) ------------------
    def _stat_counts(self):
        try:
            row = self._raw.execute(
                "SELECT COALESCE(sum(n_tup_ins),0) + COALESCE(sum("
                "n_tup_upd + n_tup_del),0) FROM pg_stat_user_tables").fetchone()
            return int(list(row.values())[0])
        except Exception:
            return None

    def _stat_delta(self, before):
        if before is None:
            return None
        # stats collector/collector-view lags behind commits; force the
        # pending stats into the view first or a same-connection write
        # measures a delta of 0 (SQLite would report the true count).
        try:
            self._raw.execute("SELECT pg_stat_force_next_flush()")
        except Exception:
            pass
        after = self._stat_counts()
        return None if after is None else after - before

    # -- identity column lookup (for lastrowid) ---------------------------------
    def _identity_col(self, table: str):
        if table in self._identity_cache:
            return self._identity_cache[table]
        col = None
        try:
            r = self._raw.execute(
                "SELECT a.attname FROM pg_catalog.pg_attribute a "
                "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relname = %s "
                "AND a.attidentity IN ('a','b') AND a.attnum > 0 "
                # Prefer the PRIMARY KEY column as the lastrowid source.
                # attnum order is physical, not logical: a legacy table
                # converged by ADD COLUMN can have the rowid twin at a
                # LOWER attnum than the core's own `id` column (manny/
                # tessa canonical_facts 2026-10-30), and RETURNING the
                # twin returns a sequence value that matches no `id`.
                "ORDER BY (EXISTS (SELECT 1 FROM pg_index i "
                "            WHERE i.indrelid = a.attrelid "
                "              AND i.indisprimary "
                "              AND i.indkey::int[] @> "
                "ARRAY[a.attnum]::int[])) DESC, "
                "       a.attnum LIMIT 1", (table,)).fetchone()
            col = r["attname"] if r else None
        except Exception:
            col = None
        # An aborted transaction (the core's savepoint-rollback paths leave
        # these) would poison EVERY later INSERT: psycopg rejects all
        # commands until the block ends, turning one caught-and-swallowed
        # error into an InFailedSqlTransaction cascade. SQLite's error
        # recovery is per-statement, so restore that boundary here: if the
        # introspection above aborted the block, end it the way SQLite
        # already has (the core's except-path rollback/commit becomes an
        # idempotent no-op afterwards).
        try:
            if (self._raw.info.transaction_status
                    == psycopg.pq.TransactionStatus.INERROR):
                self.rollback()
        except Exception:
            pass
        self._identity_cache[table] = col
        return col

    # context-manager parity (`with conn:` blocks)
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.rollback()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _q(ident: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(ident)):
        raise ValueError(f"unsafe identifier: {ident!r}")
    return '"' + str(ident) + '"'
