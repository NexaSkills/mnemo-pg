"""mnemo-pg connection factory — native PostgreSQL, schema routing only.

There is NO SQLite backend here. SQLite storage is retired as an
implementation detail: this factory opens PostgreSQL connections
(one private schema per agent profile, plus the shared surface) and
nothing else. MNEMO_BACKEND is honoured ONLY as a sanity assertion —
if someone pins it to 'sqlite' we raise, because serving from a
database we deliberately deleted looks like silent amnesia, not an
outage. `:memory:` still returns real SQLite because the vendored core
uses it as a transient in-process scratchpad inside single functions
(never a durable bank), and PostgreSQL has no equivalent there.

Stores call connect(db_path) instead of sqlite3.connect(...); the
returned NativeConn exposes the sqlite3-like surface the stores use
(execute, executescript, executemany, cursor, commit, rollback,
row_factory assignment, context-manager) while every statement that
reaches the server is native PG SQL.

Schema routing:
  - MNEMO_PG_SCHEMA wins (scripts and probes),
  - $HERMES_HOME (.../profiles/<p>) -> mnemosyne_<profile>,
  - no profiles dir -> mnemosyne_default,
  - shared-bank paths -> mnemosyne_shared.

Shape (tables/FTS/vectors) is provisioned by scripts/provision_schema.py
via scripts/native_ddl.py — never on the connection path. The only DDL
run from connect() is the date-parity self-heal: the core's temporal
predicates call julianday()/strftime()/datetime()/date(), and these
functions are cheap, idempotent CREATE OR REPLACEs that must never be
missing from a live schema.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path


def backend_for() -> str:
    """postgres, always (see module docstring)."""
    val = (os.environ.get("MNEMO_BACKEND") or "").strip().lower()
    if val in ("", "postgres", "postgresql", "pg"):
        return "postgres"
    raise RuntimeError(
        f"mnemo-pg: MNEMO_BACKEND={val!r} is not supported — SQLite storage "
        "is retired; unset the variable (postgres is the hard default)")


def _pg_dsn() -> str:
    dsn = os.environ.get("MNEMO_PG_DSN")
    if not dsn:
        # Mirror check_all.dsn(): walk up from HERMES_HOME reading the
        # MNEMO_PG_DSN= lines in .env files, then fall back to the fleet
        # cluster convention (private PG beside the shared ~/.hermes root,
        # socket dir ~/lcm-pg/run, port 54329, role lcm01). The old walk
        # never looked in the ~/.hermes root itself: a process whose env
        # carries only a profile-dir HERMES_HOME and no inherited DSN
        # (nohup/setsid/detached contexts) resolved the dsn to None and
        # sqlite3.connect silently RESURRECTED <home>/mnemosyne/data/
        # mnemosyne.db instead of failing (audit_log incident, 2026-10-30
        # 08:47:48). With a routeable DSN, NativeConn fails LOUD instead.
        roots: list[Path] = []
        home_env = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        p = Path(home_env)
        roots.extend([p, *p.parents])
        roots.append(Path.home() / ".hermes")
        seen_roots: set[Path] = set()
        for cand in roots:
            try:
                if cand in seen_roots:
                    continue
                seen_roots.add(cand)
            except TypeError:
                pass
            env = cand / ".env"
            if env.is_file():
                for line in env.read_text().splitlines():
                    if line.startswith("MNEMO_PG_DSN="):
                        dsn = line.split("=", 1)[1].strip()
                        break
            if cand.name == ".hermes":
                break
    if not dsn:
        # fleet default: the private lcm-pg cluster (beez layout first,
        # ~/.hermes/lcm-pg layout as fallback)
        for run in (Path.home() / "lcm-pg" / "run",
                    Path.home() / ".hermes" / "lcm-pg" / "run"):
            if run.is_dir():
                dsn = f"host={run} port=54329 dbname=lcm user=lcm01"
                break
    if not dsn:
        raise RuntimeError("mnemo-pg: MNEMO_PG_DSN is not set and no lcm-pg "
                           "cluster socket dir exists on this host")
    return dsn


def schema_for_profile(hermes_home: str | None) -> str:
    """mnemosyne_<profile> from HERMES_HOME (.../profiles/<p>), else
    mnemosyne_default."""
    if hermes_home:
        p = Path(hermes_home)
        if p.parent.name == "profiles":
            return "mnemosyne_" + p.name.lower().replace("-", "_")
    return "mnemosyne_default"


def _live_profile_home() -> str | None:
    """Context-local HERMES_HOME from hermes_constants (set by the multiplexer
    per routed turn via contextvar). os.environ is the LAUNCH home only, so a
    multiplexed gateway would otherwise route every profile's turns into one
    schema. Falls back to the process env when Hermes is absent."""
    try:
        import hermes_constants  # noqa: PLC0415

        home = hermes_constants.get_hermes_home()
        if home:
            return str(home)
    except Exception:
        pass
    return None


def assert_pg_conn(beam: object, identity: str = "") -> None:
    """Fail LOUD unless a beam's live connection is PostgreSQL-backed.

    NO-FALLBACKS directive (Jan, 2026-10-31): memory must only ever run on PG.
    A sqlite3 connection reaching the core means binding/backstops failed; we
    raise so provider init degrades to the visible UNAVAILABLE banner instead
    of silently resurrecting a .db store.
    """
    conn = None
    for attr in ("_conn", "conn", "_beam", "memory", "_memory"):
        cand = getattr(beam, attr, None)
        while cand is not None and not hasattr(cand, "execute") and hasattr(cand, "__dict__"):
            cand = getattr(cand, "_conn", None) or getattr(cand, "conn", None)
        if cand is not None and hasattr(cand, "execute") and hasattr(cand, "cursor"):
            conn = cand
            break
        conn = None
    if conn is None:
        raise RuntimeError(
            f"mnemo-pg FAIL-LOUD: no DB connection found on memory beam (identity={identity!r}); "
            "refusing to run memory without a verifiable PG connection")
    from .native_conn import NativeConn

    target = conn
    # unwrap one layer of common wrappers (Connection-in-Holder patterns)
    for attr in ("_conn", "conn"):
        inner = getattr(target, attr, None)
        if inner is not None and hasattr(inner, "execute"):
            target = inner
            break
    if isinstance(target, NativeConn):
        return
    raise RuntimeError(
        f"mnemo-pg FAIL-LOUD: memory beam connection is "
        f"{type(target).__module__}.{type(target).__name__}, not PostgreSQL NativeConn "
        f"(identity={identity!r}). PG is the ONLY sanctioned store; refusing to serve memory.")


def _store_schema(db_path: str | None = None) -> str:
    schema = os.environ.get("MNEMO_PG_SCHEMA")
    if not schema:
        schema = schema_for_profile(_live_profile_home() or os.environ.get("HERMES_HOME"))
    s0 = str(db_path or "")
    if "shared" in s0.lower():
        # shared cross-agent surface -> shared schema (override-aware)
        schema = os.environ.get("MNEMO_SHARED_SCHEMA", "mnemosyne_shared")
    return schema


_QIDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _qident(schema: str) -> str:
    """Safe quoted schema identifier (alnum/underscore only)."""
    s = str(schema)
    if not _QIDENT_RE.fullmatch(s):
        raise ValueError(f"unsafe schema identifier: {s!r}")
    return '"' + s + '"'


def _ensure_date_fns(base) -> None:
    """SQLite julianday()/strftime()/datetime()/date()/time() parity
    functions (temporal-validity predicates in the core). Definitions live
    in the canonical parity modules so provisioning and live paths never
    diverge."""
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
                "SELECT p.oid FROM pg_catalog.pg_proc p "
                "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname=current_schema() AND p.proname='date' "
                "AND p.pronargs=1 AND pg_get_function_arguments(p.oid) "
                "ILIKE '%text%' "
                "AND prosrc LIKE '%to_timestamp%' "
                "AND prosrc NOT LIKE '%pg_catalog%' "
                "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_proc q "
                "  JOIN pg_catalog.pg_namespace m ON m.oid=q.pronamespace "
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


def connect(db_path: str | None = None, **kwargs) -> object:
    """Connection entry point for store modules. PG for everything durable;
    real SQLite ONLY for the core's transient `:memory:` scratchpad."""
    s = str(db_path)
    if s == ":memory:" or (s.startswith("file:") and ":memory:" in s):
        return sqlite3.connect(":memory:", uri=s.startswith("file:"))
    backend_for()
    from .native_conn import NativeConn
    mode = kwargs.pop("mode", None) or kwargs.pop("_mnemo_mode", None)
    query_only = (mode == "ro") or bool(kwargs.pop("query_only", False))
    kwargs.pop("check_same_thread", None)
    kwargs.pop("timeout", None)
    kwargs.pop("detect_types", None)
    kwargs.pop("isolation_level", None)
    kwargs.pop("cached_statements", None)
    uri = kwargs.pop("uri", None)
    if uri and s.startswith("file:"):
        # file: URI with a real path — the store means an on-disk bank;
        # route by the path component (query args dropped: no readonly=,
        # mode= flags are honoured via query_only above).
        s = s[5:].split("?", 1)[0]
    schema = _store_schema(s if s else None)
    conn = NativeConn(_pg_dsn(), schema, query_only=query_only)
    if not query_only:
        _ensure_date_fns(conn)
    return conn
