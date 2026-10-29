"""mnemo-pg connection factory — backend selection + schema routing.

Set MNEMO_BACKEND=postgres (plus MNEMO_PG_DSN) to route ALL store connections
to PostgreSQL (one schema per profile, see postgres.py). MNEMO_BACKEND=sqlite
selects the upstream SQLite behaviour; `:memory:` databases always use real
SQLite regardless of backend.

Stores call connect(db_path) instead of sqlite3.connect(...); the returned
object exposes the sqlite3 surface the stores use (execute, executescript,
executemany, cursor, commit, row_factory assignment).
"""

from __future__ import annotations

import os
import re
import sqlite3  # noqa: F401  (sqlite path falls through to it in callers)
from pathlib import Path


# fts_table, content_table, indexed_column — mirrors the vendored core's FTS5
# FtsSpec declarations (mnemosyne/core/beam.py). ensure_fts names every PG
# object after (table, column), so these never collide across specs.
_FTS_SPECS = (
    ("fts_working", "working_memory", "content"),
    ("fts_episodes", "episodic_memory", "content"),
    ("fts_facts", "facts", "object"),
)

# The core's native code targets the FTS5 shadow-table NAMES directly
# (SELECT ... FROM fts_working WHERE fts_working MATCH ?). On PG the index
# lives in a tsvector column on the content table, so the shadow name is
# exposed as a view over that column.
_FTS_SHADOW_VIEWS = {
    "working_memory": "fts_working",
    "episodic_memory": "fts_episodes",
    "facts": "fts_facts",
}


def backend_for() -> str:
    """Resolved backend name. Default is postgres: an unset MNEMO_BACKEND must
    not silently diverge from the fleet standard. 'sqlite' only via explicit
    opt-out."""
    val = (os.environ.get("MNEMO_BACKEND") or "").strip().lower()
    if val in ("postgres", "postgresql", "pg"):
        return "postgres"
    if val in ("sqlite", "sqlite3"):
        return "sqlite"
    if val:
        # Unknown value: refuse rather than silently pick a backend.
        raise RuntimeError(
            f"MNEMO_BACKEND={val!r} is not recognised (postgres|sqlite)")
    return "postgres"


def _pg_dsn() -> str:
    dsn = os.environ.get("MNEMO_PG_DSN")
    if not dsn:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        p = Path(home) / ".env"
        if p.is_file():
            for line in p.read_text().splitlines():
                if line.startswith("MNEMO_PG_DSN="):
                    dsn = line.split("=", 1)[1].strip()
                    break
    if not dsn:
        # fleet default: the private lcm-pg cluster
        run = Path.home() / "lcm-pg" / "run"
        if run.is_dir():
            dsn = f"host={run} port=54329 dbname=lcm user=lcm01"
    if not dsn:
        raise RuntimeError("MNEMO_BACKEND=postgres but MNEMO_PG_DSN is not set")
    return dsn


def schema_for_profile(hermes_home: str | None) -> str:
    """mnemosyne_<profile> from HERMES_HOME (.../profiles/<p>), else
    mnemosyne_default."""
    if hermes_home:
        p = Path(hermes_home)
        if p.parent.name == "profiles":
            return "mnemosyne_" + p.name.lower().replace("-", "_")
    return "mnemosyne_default"


def _store_schema(db_path: str | None = None) -> str:
    schema = os.environ.get("MNEMO_PG_SCHEMA")
    if not schema:
        home = os.environ.get("HERMES_HOME")
        schema = schema_for_profile(home)
    s0 = str(db_path or "")
    if "shared" in s0.lower():
        # shared cross-agent surface -> shared schema (override-aware)
        schema = os.environ.get("MNEMO_SHARED_SCHEMA", "mnemosyne_shared")
    return schema


def _ensure_fts_shadow_views(base) -> None:
    # The core queries shadow NAMES directly (SELECT id, rank FROM fts_working
    # WHERE fts_working MATCH ?). On PG the index lives in a trigger-maintained
    # tsvector column on the content table, so expose a view named after the
    # shadow that carries: the indexed content column, the content table's
    # primary key under `id`, SQLite's implicit `rowid` alias, and a real
    # `rank` (ts_rank; SQLite BM25 ranks are negative, PG ts_rank positive
    # with "higher is better" — so negate and scale to keep SQLite's
    # ascending-is-better contract).
    from . import fts_pg
    try:
        from . import rowid_rewrite
        rowid_rewrite.ensure_rowid_shims(base)
    except Exception:
        try:
            base.rollback()
        except Exception:
            pass
    for fts_t, content, col in _FTS_SPECS:
        view = fts_t
        try:
            tcol = fts_pg.ts_colname(col)
            rowid_src = '"rowid"' if _table_has_real_rowid(base, content) \
                else f'"{content}"."{content}__rowid"'
            pk = _pk_column(base, content)
            rank = ("COALESCE(-ts_rank(\"%s\", ''::tsquery), 0)::float8"
                    % tcol)
            # PG forbids CREATE OR REPLACE VIEW from changing existing
            # column names (a stale facade may project only the content
            # column), so drop first — the view holds no state.
            base.execute('DROP VIEW IF EXISTS "%s"' % view)
            base.execute(
                'CREATE OR REPLACE VIEW "%s" (%s, %s, %s, %s) AS '
                'SELECT %s AS "rowid", %s AS "rank", '
                '"%s" AS "%s", "%s"."%s" AS "id" FROM "%s"'
                % (view, "rowid", "rank", col, "id",
                   rowid_src, rank, col, col, content, pk, content)
            )
        except Exception:
            base.rollback() if hasattr(base, "rollback") else None


def _table_has_real_rowid(base, table: str) -> bool:
    try:
        row = base.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "AND column_name = 'rowid' LIMIT 1", (table,)).fetchone()
        return row is not None
    except Exception:
        return False


def _pk_column(base, table: str) -> str:
    # NOTE: do NOT use `a.attnum = ANY(i.indkey)` here. indkey is int2vector;
    # the comparison coerces attnum to int2 and silently matches unrelated
    # system columns (returns e.g. 'oid'), producing broken facade views.
    row = base.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_class t ON t.oid = i.indrelid "
        "JOIN unnest(i.indkey::int[]) WITH ORDINALITY AS k(attnum, ord) "
        "  ON true "
        "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE n.nspname = current_schema() AND t.relname = %s "
        "AND i.indisprimary AND k.attnum > 0 "
        "ORDER BY k.ord LIMIT 1", (table,)).fetchone()
    if row is None:
        return "rowid"
    return row[0] if not isinstance(row, dict) else next(iter(row.values()))


def _ensure_fts_tables(base) -> tuple:
    from . import fts_pg

    out = []
    for fts_t, content_t, col in _FTS_SPECS:
        try:
            fts_pg.ensure_fts(base, content_t, col)
            out.append((fts_t, content_t, col))
        except Exception:
            try:
                base.rollback()
            except Exception:
                pass
    try:
        _ensure_date_fns(base)
    except Exception:
        pass
    # Shadow views must be (re)built on EVERY ensure, not just at connect:
    # provisioning or an earlier boot may have left stale one-column views,
    # and the rewrite layer running CREATE OR REPLACE during a live statement
    # can be mangled. An idempotent rebuild here is the cheap self-heal.
    try:
        _ensure_fts_shadow_views(base)
    except Exception:
        try:
            base.rollback()
        except Exception:
            pass
    return tuple(out)


def _qident(schema: str) -> str:
    """Safe quoted schema identifier (alnum/underscore only)."""
    s = str(schema)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s):
        raise ValueError(f"unsafe schema identifier: {s!r}")
    return '"' + s + '"'


def _ensure_date_fns(base) -> None:
    """SQLite julianday()/strftime()/datetime()/date()/time() parity
    functions (temporal-validity predicates in the core). Definitions live
    in the canonical parity modules so provision/live paths never diverge."""
    from . import date_fns
    for fn in date_fns.FUNCTIONS:
        try:
            base.execute(fn)
        except Exception:
            try:
                base.rollback()
            except Exception:
                pass
    try:
        schema = base.execute("SELECT current_schema()").fetchone()
        schema = (schema[0] if not isinstance(schema, dict)
                  else next(iter(schema.values())))
    except Exception:
        try:
            base.rollback()
        except Exception:
            pass
        schema = None
    if schema:
        from . import date_fns_extra
        for fn in date_fns_extra.sql_for(schema):
            try:
                base.execute(fn)
            except Exception:
                try:
                    base.rollback()
                except Exception:
                    pass
        # Self-heal a stale schema-local date(text) from an older boot: if
        # its body delegates to a schema-qualified to_timestamp that does
        # not exist in this schema (PG keeps it in pg_catalog), rewrite the
        # body to delegate to <schema>.datetime instead. CREATE OR REPLACE
        # with the canonical DDL would not help — the body is a literal
        # string that only provisions at creation time.
        try:
            bad = base.execute(
                "SELECT p.oid FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname=current_schema() AND p.proname='date' "
                "AND p.pronargs=1 AND pg_get_function_arguments(p.oid) "
                "ILIKE '%text%' "
                "AND prosrc LIKE '%to_timestamp%' "
                "AND prosrc NOT LIKE '%pg_catalog%' "
                "AND NOT EXISTS (SELECT 1 FROM pg_proc q "
                "  JOIN pg_namespace m ON m.oid=q.pronamespace "
                "  WHERE m.nspname=current_schema() AND q.proname='to_timestamp')",
            ).fetchone()
            if bad:
                body = ("BEGIN RETURN " + _qident(schema) + ".datetime(x); END;")
                base.execute(
                    "CREATE OR REPLACE FUNCTION " + _qident(schema)
                    + ".date(x text) RETURNS text LANGUAGE plpgsql "
                    "IMMUTABLE AS " + "$fn$" + body + "$fn$")
        except Exception:
            try:
                base.rollback()
            except Exception:
                pass


def _existing_fts_tables(base) -> tuple:
    """RO handle: report already-provisioned specs without running DDL."""
    from . import fts_pg

    out = []
    for fts_t, content_t, col in _FTS_SPECS:
        try:
            if fts_pg.has_fts(base, content_t, col):
                out.append((fts_t, content_t, col))
        except Exception:
            pass
    return tuple(out)


def connect(db_path: str | None = None, **kwargs) -> object:
    """Connection entry point for store modules under the PG backend."""
    backend = backend_for()
    if backend == "postgres" and str(db_path) != ":memory:":
        return _connect_postgres(db_path, kwargs)
    # sqlite passthrough (uri-aware)
    if "uri" not in kwargs and str(db_path).startswith("file:"):
        kwargs["uri"] = True
    return sqlite3.connect(db_path, **kwargs)


def _connect_postgres(db_path, kwargs):
    from . import fts_pg
    from . import vec_store_meta
    from .postgres import connect as pg_connect
    from .fts_conn import FtsConnection, set_spec

    mode = kwargs.pop("mode", None) or kwargs.pop("_mnemo_mode", None)
    if mode == "ro":
        query_only = True
    else:
        query_only = bool(kwargs.pop("query_only", False))
    kwargs.pop("check_same_thread", None)
    kwargs.pop("timeout", None)
    schema = _store_schema(str(db_path) if db_path else None)
    if query_only:
        # RO handles must not run DDL; provision on a short admin handle.
        admin = pg_connect(_pg_dsn(), schema)
        try:
            fts_pg.ensure_helper(admin)
            _ensure_fts_shadow_views(admin)
            vec_store_meta.ensure_meta_fn(admin)
        finally:
            try:
                admin.close()
            except Exception:
                pass
        base = pg_connect(_pg_dsn(), schema, query_only=True)
    else:
        base = pg_connect(_pg_dsn(), schema)
        fts_pg.ensure_helper(base)
        # The vec-regime marker (PRAGMA user_version emulation) needs its 1-row
        # meta table to exist for reads, not just for the core's marker write.
        try:
            vec_store_meta.ensure_meta_fn(base)
        except Exception:
            pass
    # Always ensure the FTS shadow views exist (this function only runs on
    # the PG backend; a bare `backend` name here used to NameError the
    # whole init path on the read-write branch).
    try:
        admin = pg_connect(_pg_dsn(), schema)
        try:
            _ensure_fts_shadow_views(admin)
            # Meta table for the PRAGMA user_version emulation (vec-regime
            # marker) must exist on every schema, not just on a marker write.
            vec_store_meta.ensure_meta_fn(admin)
        finally:
            try:
                admin.close()
            except Exception:
                pass
    except Exception:
        pass
    conn = FtsConnection(base)
    if mode == "ro":
        set_spec(conn, _existing_fts_tables(base))
    else:
        set_spec(conn, _ensure_fts_tables(base))
    return conn
