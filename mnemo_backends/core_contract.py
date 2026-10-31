"""Connect-time self-heal: guarantee + validate the core's declared columns.

Root cause class (2026-10-31 fleet-wide): the vendored core's init validates
every column it declares via _add_column_if_missing (strict type/NOT NULL/
DEFAULT compare) and every CREATE INDEX it issues requires its column to
exist. A schema provisioned by scripts/native_ddl.py (or an old mnemosyne
generation) diverges in both directions: missing columns -> UndefinedColumn;
present-but-wrong columns -> OperationalError 'schema mismatch'. Either kills
provider init (_beam=None -> silent memory outage on that profile).

Two passes, both GENERATED from the vendored core (no hand-maintained list,
tracks core upgrades automatically):

  A. core_contract_gen.indexed_columns()  -> ADD missing columns (IF NOT EXISTS).
     Types via native_conn._contract_type() (corrected_by -> bigint etc.).
  B. core_declared validation            -> for columns that EXIST but break
     the core's validator, apply the minimal fix, mirroring beam.py
     _column_matches semantics:
       * type: TEXT<->TIMESTAMP twins accepted; else ALTER TYPE ... USING
         (guarded: failing cast leaves the column untouched, error logged).
       * DEFAULT: core declares a DEFAULT, PG has none -> SET DEFAULT.
       * NOT NULL: core requires it, PG is nullable -> SET NOT NULL (backfill
         DEFAULT first so existing rows don't fail the validation scan);
         core doesn't require it, PG forces it -> DROP NOT NULL.
         beam.py _column_matches compares notnull EXACTLY (both directions),
         so a one-sided relax leaves the guard failing (device_id class,
         2026-10-31).

Idempotent and best-effort; failures never block the connection — the core's
own error path stays loud if a fix genuinely can't apply. Schema-local by
search_path (NativeConn sets it before calling; raw catalog queries pin
current_schema() so public shadows are never touched).
"""

import re

from .core_contract_gen import indexed_columns
from .core_declared import declared_columns

# per-process memory: schemas already validated this process (one catalog
# scan per schema per process, mirroring _HEALED_CONTRACT_SCHEMAS).
_HEALED: set = set()

_SQLITE_PG = {"TEXT": "text", "INTEGER": "bigint", "REAL": "double precision",
             "BLOB": "bytea", "TIMESTAMP": "timestamp with time zone"}

# (have -> want) casts we consider safe for an index/guard heal: widening or
# explicit text round-trips. Anything else logs and skips (converge2 /
# converge_guard_decls handle those at deploy time, loudly).
_CAST_OK = {("text", "bigint"), ("text", "double precision"), ("bigint", "text"),
            ("double precision", "text"), ("text", "bytea"), ("bytea", "text"),
            ("bigint", "double precision"), ("double precision", "bigint"),
            ("integer", "bigint"), ("bigint", "integer"),
            ("integer", "double precision"), ("double precision", "integer")}


def _want_pg(col, sqlite_decl):
    from .native_conn import _contract_type
    # validator-critical names (corrected_by INTEGER guard) win over decl map
    t = _contract_type(col)
    if t != "text":
        return t
    m = re.match(r"([A-Za-z]+)", sqlite_decl or "TEXT")
    return _SQLITE_PG.get((m.group(1).upper() if m else "TEXT"), "text")


def ensure_core_contract_columns(raw, contract_type=None):
    """Pass A: add every core-declared column missing from the active schema."""
    try:
        from .native_conn import _q
        ct = contract_type or _contract_type_default()
        extra = {}
        try:
            for table, cols in declared_columns().items():
                extra[table] = set(cols)
        except Exception:
            pass
        try:
            for table, cols in indexed_columns().items():
                extra.setdefault(table, set()).update(cols)
        except Exception:
            pass
        for table, cols in sorted(extra.items()):
            for col in sorted(cols):
                try:
                    raw.execute(
                        "ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s %s"
                        % (_q(table), _q(col), ct(col)))
                except Exception:
                    pass  # table absent or locked: not ours
    except Exception as e:
        # heal must never block connect, but silence here hid real drift
        # (device_id class, 2026-10-31): log loudly instead.
        try:
            import logging
            sch = raw.execute("select current_schema()").fetchone()
            logging.getLogger("mnemo_pg.core_contract").warning(
                "core-contract ADD pass failed for schema=%s: %s: %s",
                list(sch.values())[0] if sch else "?", type(e).__name__, e)
        except Exception:
            pass


def validate_core_contract(raw):
    """Pass B: fix existing columns that would fail the core's validator.

    Returns count of fixes (0 in steady state). Best-effort; never raises.
    """
    fixes = 0
    try:
        from .native_conn import _q
        decls = declared_columns()
        if not decls:
            return 0
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
                       for t, col, ty, nn, dflt in (tuple(r.values()) if hasattr(r, "values") else r for r in c.fetchall())}
        for table, cols in decls.items():
            for col, decl in cols.items():
                have = present.get((table, col))
                if have is None:
                    continue
                htype, hnn, hdflt = have
                want = _want_pg(col, decl)
                # type compare: TEXT<->TIMESTAMP twins accepted (beam.py rule)
                ok = (htype == want
                      or (htype.startswith("timestamp") and want.startswith("timestamp"))
                      or ({htype.split()[0], want.split()[0]} == {"text", "timestamp"}))
                if not ok:
                    if (htype, want) not in _CAST_OK:
                        continue
                    try:
                        with raw.cursor() as c:
                            c.execute(
                                "ALTER TABLE %s ALTER COLUMN %s TYPE %s USING (%s::%s)"
                                % (_q(table), _q(col), want, _q(col), want))
                        fixes += 1
                    except Exception:
                        try:
                            raw.rollback()
                        except Exception:
                            pass
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
                        with raw.cursor() as c:
                            c.execute("ALTER TABLE %s ALTER COLUMN %s SET DEFAULT %s"
                                      % (_q(table), _q(col), want_dflt))
                        fixes += 1
                    except Exception:
                        try:
                            raw.rollback()
                        except Exception:
                            pass
                if re.search(r"\bNOT NULL\b", decl or "", re.I):
                    if not hnn:
                        # core demands NOT NULL, PG is nullable. Backfill the
                        # declared DEFAULT (else existing NULLs fail the scan),
                        # then SET NOT NULL. Both are metadata-cheap.
                        try:
                            with raw.cursor() as c:
                                if want_dflt is not None:
                                    c.execute("UPDATE %s SET %s = %s WHERE %s IS NULL"
                                              % (_q(table), _q(col), want_dflt, _q(col)))
                                c.execute("ALTER TABLE %s ALTER COLUMN %s SET NOT NULL"
                                          % (_q(table), _q(col)))
                            fixes += 1
                        except Exception:
                            try:
                                raw.rollback()
                            except Exception:
                                pass
                elif hnn:
                    try:
                        with raw.cursor() as c:
                            c.execute("ALTER TABLE %s ALTER COLUMN %s DROP NOT NULL"
                                      % (_q(table), _q(col)))
                        fixes += 1
                    except Exception:
                        try:
                            raw.rollback()
                        except Exception:
                            pass
    except Exception:
        pass
    return fixes


_CT = None


def _contract_type_default():
    global _CT
    if _CT is None:
        from .native_conn import _contract_type
        _CT = _contract_type
    return _CT
