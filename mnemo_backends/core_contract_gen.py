"""Generate the core-init indexed-column contract from the vendored core itself.

Source of truth: the CREATE INDEX IF NOT EXISTS statements the core runs on
every connect (core/beam.py et al.). Any column one of those indexes
references must exist in the schema, or provider init dies with
UndefinedColumn. The deploy script's schema contract must cover them all;
native_ddl provably lacks several (memoria_facts.key, memory_events.*,
episodic_memory.author_id/channel_id/tier/source/event_date, ...).

Types: delegate to the connector's own _contract_type() so a column that the
core's _add_column_if_missing guards validate (corrected_by bigint, importance
double precision, recall_count bigint, ...) gets the identical type here —
otherwise the core's own schema validator mismatches. Everything else: text,
the sync-table lineage used across converged schemas.

Tables with a PRIMARY KEY in the native DDL (e.g. memory_events.id) may not
gain NOT NULL columns by ALTER — the core tolerates NULL columns for its
indexes (its own _add_column_if_missing adds columns without NOT NULL), so
plain nullable adds match both the core's behavior and what already exists on
healthy hosts (debbie's memory_events has these columns, nullable).
"""

import re
from pathlib import Path

_IDX_RE = re.compile(
    r"CREATE INDEX IF NOT EXISTS \w+ ON (\w+)\(([^)]+)\)")

# sqlite type -> PG type (only types seen in core CREATE/ALTER bodies)
_SQLITE_TYPES = {
    "text": "text",
    "integer": "bigint",
    "int": "bigint",
    "real": "double precision",
    "blob": "bytea",
}


def _vendor_core_dir():
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "vendor" / "mnemosyne" / "core",
                 here / "vendor" / "mnemosyne" / "core"):
        if cand.is_dir():
            return cand
    return None


def _alter_bodies(src):
    """table, col, sqlite-type from _add_column_if_missing(...) calls."""
    out = {}
    for m in re.finditer(
            r'_add_column_if_missing\(\s*\w+\s*,\s*"(\w+)"\s*,\s*"(\w+)"\s*,'
            r'\s*(?:"([^"]+)"|([^,()]+)\.split\([^\n]*\))', src):
        table, col = m.group(1), m.group(2)
        lit = m.group(3)
        if lit is None:
            # ddl.split(" ", 1)[1] — resolve the local ddl dict literal if present
            lit = _resolve_split_default(src, m.start())
        if lit:
            t = re.match(r"(\w+)", lit.strip())
            if t:
                out[(table, col)] = t.group(1).lower()
    return out


def _resolve_split_default(src, pos):
    """Find `"col": "TYPE ..."` in the nearest preceding dict literal."""
    window = src[max(0, pos - 1500):pos]
    return None  # dict-literal types default to text; guards still fix types


def indexed_columns(core_dir=None):
    """dict table -> set(columns) across all core-init CREATE INDEX statements."""
    core_dir = core_dir or _vendor_core_dir()
    result = {}
    if core_dir is None:
        return result
    for py in sorted(core_dir.glob("*.py")):
        try:
            src = py.read_text()
        except OSError:
            continue
        for m in _IDX_RE.finditer(src):
            table, cols = m.group(1), m.group(2)
            for col in cols.split(","):
                col = col.strip().split("(")[0].strip()  # expression idx: skip funcs
                col = re.sub(r"\s+(ASC|DESC)$", "", col, flags=re.I)
                if col and not col.isdigit() and col.isidentifier():
                    result.setdefault(table, set()).add(col)
        for (table, col), t in _alter_bodies(src).items():
            result.setdefault(table, set()).add(col)
    return result


def contract_statements(schema, core_dir=None, contract_type=None):
    """Idempotent ALTERs guaranteeing every core-indexed column per schema."""
    from .native_conn import _q, _contract_type as default_ct
    ct = contract_type or default_ct
    stmts = []
    for table, cols in sorted(indexed_columns(core_dir).items()):
        for col in sorted(cols):
            stmts.append(
                "ALTER TABLE %s.%s ADD COLUMN IF NOT EXISTS %s %s"
                % (_q(schema), _q(table), _q(col), ct(col)))
    return stmts


if __name__ == "__main__":
    import sys
    schema = sys.argv[1] if len(sys.argv) > 1 else "mnemosyne_default"
    for s in contract_statements(schema):
        print(s + ";")
