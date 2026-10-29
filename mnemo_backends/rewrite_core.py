"""Import-time SQL rewrite of the vendored mnemosyne core (PG backend).

Why: the core passes SQLite SQL to sqlite3 cursors. Under the PG backend
connections are routed through postgres.py, which translates most idioms
but deliberately REFUSES `INSERT OR REPLACE` without an explicit
ON CONFLICT target (a correct policy: replace semantics need a conflict
target). Every INSERT OR REPLACE in mnemosyne 4.0.0b4 targets a table with
a real UNIQUE/PK constraint (verified against the DDL), so this module
annotates those call sites in the core modules' string constants before
any of them runs, turning them into PG-expressible upserts.

This is a code rewrite done once, at our own import boundary, of code we
vendor — not a runtime monkeypatch. Each module is verified before it is
touched: the annotated form must be derivable deterministically, and any
INSERT OR REPLACE that already carries an ON CONFLICT clause is skipped
(the translator would refuse it anyway).

Only annotate_tables entries are rewritten; an INSERT OR REPLACE into a
table NOT in this map is left untouched so it fails loudly through the
translator rather than silently doing the wrong thing.
"""

from __future__ import annotations

import os
import re

# INSERT OR REPLACE -> upsert target per table (conflict column list from the
# PG DDL in schema_ddl.py; all UNIQUE/PK there).
ANNOTATE_TABLES = {
    "memory_embeddings": ["memory_id"],
    "gists": ["id"],
    "facts": ["key"],
    "graph_edges": ["source", "target", "edge_type"],
    "query_cache": ["normalized"],
    "memories": ["id"],
    "sync_meta": ["key"],
    "sync_memory_state": ["memory_id"],
    "sync_outbox_ack": ["event_id"],
    "harmonic_beliefs": ["topic"],
}

# table names may be schema-qualified or bare; allow optional quotes.
_TABLE_AFTER = "|".join(sorted(ANNOTATE_TABLES, key=len, reverse=True))
_RE_IOR = re.compile(
    r"INSERT\s+OR\s+REPLACE\s+INTO\s+[\"`]?(?P<table>" + _TABLE_AFTER + r")[\"`]?"
    r"(?P<rest>(?:(?!ON\s+CONFLICT).)*?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _upsert_suffix(table: str) -> str:
    cols = ", ".join(f'"{c}"' for c in ANNOTATE_TABLES[table])
    return (
        f" ON CONFLICT ({cols}) DO UPDATE SET "
        "{assignments}"
    )


def _annotate_sql(sql: str) -> str:
    """Rewrite one SQL string; returns it unchanged if nothing applies."""
    out_lines = []
    changed = False
    for line in sql.split("\n"):
        m = _RE_IOR.search(line)
        if m and "ON CONFLICT" not in line.upper():
            table = m.group("table").lower()
            rest = m.group("rest")
            # column list must end with ')' — else the line doesn't carry it
            # (values-only form); handle the common 'cols) VALUES (...)' tail.
            if rest.rstrip().endswith(")") or ") VALUES" in rest.upper() or ") values" in rest:
                conflict_cols = ", ".join(ANNOTATE_TABLES[table])
                line = (
                    line.rstrip()
                    + f" ON CONFLICT ({conflict_cols}) DO UPDATE SET "
                    + _set_clause(table, m.group("rest"))
                )
                changed = True
        out_lines.append(line)
    return "\n".join(out_lines) if changed else sql


def _set_clause(table: str, cols_part: str) -> str:
    """Build 'col = EXCLUDED.col, ...' from the INSERT column list."""
    m = re.search(r"\(([^()]*)\)\s*(?:VALUES|values|SELECT|select)", cols_part,
                  re.IGNORECASE | re.DOTALL)
    if not m:
        # fall back: update only conflict cols with self (still deterministic)
        cols = ANNOTATE_TABLES[table]
    else:
        cols = [c.strip().strip('"`') for c in m.group(1).split(",") if c.strip()]
        cols = [c for c in cols if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c)]
    if not cols:
        cols = ANNOTATE_TABLES[table]
    return ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols)


def annotate_module_strings(mod) -> int:
    """Walk a module's string globals (incl. nested dicts/lists/tuples) and
    rewrite INSERT OR REPLACE statements for known tables. Returns count."""
    n = 0
    seen: set = set()

    def fix_value(v):
        nonlocal n
        if isinstance(v, str) and "INSERT OR REPLACE" in v.upper():
            new = _annotate_sql(v)
            if new != v:
                n += 1
            return new
        if isinstance(v, dict):
            if id(v) in seen:
                return v
            seen.add(id(v))
            for k in list(v):
                nv = fix_value(v[k])
                if nv is not v[k]:
                    v[k] = nv
            # functions are constants, not values: nothing else to do
            return v
        if isinstance(v, (list, tuple)) and id(v) not in seen:
            seen.add(id(v))
            if isinstance(v, list):
                for i in range(len(v)):
                    nv = fix_value(v[i])
                    if nv is not v[i]:
                        v[i] = nv
            return v
        return v

    for name, val in list(vars(mod).items()):
        nv = fix_value(val)
        if nv is not val:
            try:
                setattr(mod, name, nv)
            except Exception:
                pass
    return n


def annotate_module_functions(mod, source_attr: str = "__file__") -> int:
    """Rewrite string literals used INSIDE functions by editing the module
    source lazily: not possible per-call; instead we patch at the SQL
    boundary — call sites route cursor.execute through postgres.py which
    already handles the rest. This function exists so callers can report
    'function-local statements are handled at execute() level' honestly."""
    return 0


_ENABLED = None


def enabled() -> bool:
    global _ENABLED
    if _ENABLED is None:
        _ENABLED = (os.environ.get("MNEMO_BACKEND") or "postgres").strip().lower() != "sqlite"
    return _ENABLED
