#!/usr/bin/env python3
"""Native PG connection for the vendored mnemosyne core.

This is the ONLY connection entry point for postgres mode. There is no SQL
translation: statements are real PG SQL, vectors are real pgvector columns
(HNSW cosine KNN), FTS is real tsvector + GIN. The shim package under
mnemo_backends/ is NOT imported here.

What lives here (all semantics, not syntax translation):
  - connect(): psycopg connection configured to behave like the core's
    sqlite3 handles (autocommit, row factory, context-manager protocol).
  - KNN helpers: the vec MATCH ... AND k=N idiom becomes
    ORDER BY embedding <=> %s LIMIT N with distance->similarity mapping.
  - FTS helper: build a native tsquery from an FTS5-style query.
  - bootstrap(): idempotent native DDL for one schema (same contract as
    scripts/native_ddl.py; kept importable for runtime self-heal).

MNEMO_PG_DSN resolves like the shim's factory (env, then $HERMES_HOME/.env,
then the private lcm-pg cluster). MNEMO_PG_SCHEMA or $HERMES_HOME picks the
schema (mnemosyne_<profile>, or mnemosyne_shared for the shared surface).
"""
from __future__ import annotations

import functools
import os
import pathlib
import re

import psycopg
import psycopg.rows

EMBED_DIM = 384


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------

def resolve_dsn() -> str:
    d = os.environ.get("MNEMO_PG_DSN")
    if d:
        return d
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    p = pathlib.Path(home) / ".env"
    if p.is_file():
        for line in p.read_text().splitlines():
            if line.startswith("MNEMO_PG_DSN="):
                return line.split("=", 1)[1].strip()
    run = pathlib.Path.home() / "lcm-pg" / "run"
    if run.is_dir():
        return f"host={run} port=54329 dbname=lcm user=lcm01"
    raise RuntimeError("mnemo-pg: no MNEMO_PG_DSN and no lcm-pg/run socket dir")


def schema_for(home: str | None = None) -> str:
    """Schema routing: MNEMO_PG_SCHEMA wins; otherwise derive from HERMES_HOME."""
    s = os.environ.get("MNEMO_PG_SCHEMA")
    if s:
        return s
    home = home or os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    base = os.path.basename(home.rstrip("/"))
    if base == "mnemosyne":
        return "mnemosyne_shared"
    if base == ".hermes":
        return "mnemosyne_default"
    return "mnemosyne_" + re.sub(r"[^a-z0-9_]", "_", base.lower())


def connect(dsn: str | None = None, schema: str | None = None,
            home: str | None = None) -> "NativeConn":
    schema = schema or schema_for(home)
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
        raise ValueError(f"mnemo-pg: unsafe schema name {schema!r}")
    conn = psycopg.connect(dsn or resolve_dsn(), autocommit=True)
    return NativeConn(conn, schema)


class NativeCursor:
    def __init__(self, conn: "NativeConn", pcur):
        self._conn = conn
        self._cur = pcur

    def execute(self, sql: str, params=()):
        # sqlite-style ? placeholders are accepted (converted positionally);
        # %s is native psycopg. This is placeholder plumbing, not SQL rewrite.
        if "?" in sql and "%s" not in sql:
            sql = sql.replace("?", "%s")
        self._cur.execute(sql, params or ())
        return self

    def executemany(self, sql, seq):
        if "?" in sql and "%s" not in sql:
            sql = sql.replace("?", "%s")
        self._cur.executemany(sql, list(seq))
        return self

    def executescript(self, script: str):
        self._cur.execute(script)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def fetchmany(self, n=None):
        return self._cur.fetchmany(n) if n else self._cur.fetchmany()

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    @property
    def lastrowid(self):
        return None

    def close(self):
        self._cur.close()

    def __iter__(self):
        return iter(self._cur)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class NativeConn:
    """psycopg connection with sqlite-compatible ergonomics for the core.

    autocommit=True mirrors pysqlite's implicit-commit driver mode:
    single-statement commits, explicit BEGIN (via transaction()) defers.
    """

    def __init__(self, pconn, schema: str):
        self._conn = pconn
        self.schema = schema
        self._closed = False
        self.row_factory = None  # settable; None -> plain tuples
        self._apply_row_factory()
        self._conn.execute(
            "SELECT set_config('search_path', %s || ', public', false)", (schema,))

    def _apply_row_factory(self):
        rf = self.row_factory
        if rf is None:
            self._conn.row_factory = psycopg.rows.tuple_row
        elif rf is dict:
            self._conn.row_factory = psycopg.rows.dict_row
        elif callable(rf):
            # sqlite3.Row-style factory(cols)->constructor; adapt by name access.
            self._conn.row_factory = psycopg.rows.namedtuple_row
        else:
            self._conn.row_factory = psycopg.rows.tuple_row

    # -- cursors ----------------------------------------------------------
    def cursor(self):
        return NativeCursor(self, self._conn.cursor())

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def executemany(self, sql, seq):
        return self.cursor().executemany(sql, seq)

    def executescript(self, script):
        return self.cursor().executescript(script)

    # -- transactions (explicit BEGIN semantics) ---------------------------
    def transaction(self):
        return self._conn.transaction()

    def commit(self):
        pass  # autocommit driver mode: statements already committed

    def rollback(self):
        pass

    def close(self):
        if not self._closed:
            self._closed = True
            self._conn.close()

    # sqlite "with conn:" = transaction context
    def __enter__(self):
        return self._conn.transaction().__enter__()

    def __exit__(self, *exc):
        return self._conn.transaction().__exit__(*exc) if False else \
            self.__dict__.setdefault("_txn", None) and False

    def enable_load_extension(self, _flag):
        pass  # native backend loads no sqlite extensions

    def create_function(self, *_a, **_k):
        pass  # core UDF registration is a no-op on PG (SQL functions exist)

    @property
    def total_changes(self):
        return None

    # -- native feature helpers -------------------------------------------
    def knn(self, vec_table: str, query_vec, k: int,
            where: str = "", where_params=()) -> list[tuple[int, float]]:
        """Vector top-k by cosine: real pgvector KNN over the HNSW index."""
        vec = "[" + ",".join(repr(float(x)) for x in query_vec) + "]"
        sql = (
            f"SELECT rowid, 1 - (embedding <=> %s::vector) AS similarity "
            f"FROM {vec_table} {where} "
            f"ORDER BY embedding <=> %s::vector LIMIT {int(k)}")
        with self.cursor() as cur:
            cur.execute(sql, (vec, vec, *where_params))
            return [(r[0], float(r[1])) for r in cur.fetchall()]

    def fts(self, table: str, tsv_col: str, query: str, k: int = 50,
            cols: str = "*") -> list:
        """Native FTS over the tsvector GIN index; bm25-ish rank via ts_rank.

        The tsquery argument comes from the schema's mnemo_normalize_fts()
        SQL function (provisioned by native_ddl.render): FTS5 query syntax
        -> valid tsquery text (OR/AND/NOT operators, tok* prefix -> tok:*,
        phrase/NEAR collapse). search_path resolves it inside this schema.
        """
        with self.cursor() as cur:
            cur.execute(
                f"SELECT {cols}, ts_rank({tsv_col}, "
                f"to_tsquery('simple', mnemo_normalize_fts(%s))) AS rank "
                f"FROM {table} "
                f"WHERE {tsv_col} @@ to_tsquery('simple', mnemo_normalize_fts(%s)) "
                f"ORDER BY rank DESC LIMIT {int(k)}", (query, query))
            return cur.fetchall()


# ---------------------------------------------------------------------------
# FTS query normalization (FTS5 syntax -> tsquery input)
# ---------------------------------------------------------------------------

def normalize_fts_query(q: str) -> str:
    """Client-side fallback; the authoritative normalizer is the per-schema
    SQL function mnemo_normalize_fts() (see native_ddl.render) which the
    NativeConn.fts path uses. This adapter just tidies whitespace and strips
    a trailing prefix star for callers that use websearch_to_tsquery."""
    q = (q or "").strip()
    if not q:
        return ""
    q = re.sub(r'(\w+)\s*\*\s*$', r'\1', q)
    q = re.sub(r'\s+', ' ', q)
    return q


# ---------------------------------------------------------------------------
# bootstrap: native DDL (importable mirror of scripts/native_ddl.py)
# ---------------------------------------------------------------------------

def bootstrap(conn: NativeConn) -> None:
    """Idempotent native provisioning for conn.schema (delegates to the
    scripts/native_ddl.py generator; importable at runtime, no shim)."""
    scripts = pathlib.Path(__file__).resolve().parent.parent / "scripts"
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mnemo_native_ddl", scripts / "native_ddl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    conn.execute(mod.render(conn.schema))
