"""SQLite-dialect → PostgreSQL-dialect rewrites for vendor SQL.

SQLite-only spellings that reach PG through NativeConn are translated
HERE (adapter layer, alongside BEGIN IMMEDIATE, FTS5 and rowid
rewriting), keeping vendored code upstream-clean and fixing every call
site at once, including future ones:

* ``x IS NOT 'literal'`` — SQLite's null-safe "is not equal to" (PG
  removed this grammar). ``IS DISTINCT FROM`` is PG's exact equivalent,
  null-safe the same way. PG's own ``IS NOT NULL`` grammar untouched.

* ``expr COLLATE NOCASE`` — SQLite's case-insensitive marker. PG has
  no nocase collation. Comparison sites (``col = expr COLLATE NOCASE``
  or ``col COLLATE NOCASE = expr``) become ``LOWER(col) = LOWER(expr)``
  — in SQLite the COLLATE governed the whole comparison, so both sides
  fold. Pattern sites (``col LIKE expr COLLATE NOCASE``) fold only the
  right side (SQLite LIKE is case-insensitive for ASCII already; the
  fold preserves that). ORDER BY sites become ``lower(expr)`` with
  direction preserved. Bare/other sites fold the marked operand.
  SQLite NOCASE folds ASCII bytes only; LOWER folds a superset
  (non-ASCII too) — acceptable. Placeholders are wrapped, never
  duplicated or dropped.
"""

from __future__ import annotations

import re

# An operand: quoted literal, ?/$n placeholder, fn(...) call, or ident.
_OPERAND = (
    r"'(?:[^']|'')*'"
    r"|\?"
    r"|\$[1-9]\d*"
    r"|[A-Za-z_]\w*\s*\([^()]*\)"
    r"|[A-Za-z_][\w.]*"
)

_OP = (
    r"IS\s+NOT\s+DISTINCT\s+FROM\s+|IS\s+DISTINCT\s+FROM\s+|IS\s+NOT\s+|IS\s+"
    r"|NOT\s+IN\s*\(|IN\s*\(|BETWEEN\s+"
    r"|NOT\s+LIKE\s+|NOT\s+ILIKE\s+|\bLIKE\s+|\bILIKE\s+"
    r"|\bGLOB\s+|\bREGEXP\s+|\bMATCH\s+"
    r"|<=>|>=|<=|<>|!=|>>|<<|[<>=~]"
    r"|\bAND\s+|\bOR\s+|,|\("
)

_OPTR = re.compile(
    r"(?P<left>" + _OPERAND + r")\s*"
    r"(?P<op>" + _OP + r")\s*"
    r"(?P<right>" + _OPERAND + r")"
    r"(?:\s*\|\|\s*" + _OPERAND + r")*"
    r"\s+COLLATE\s+NOCASE\b",
    re.IGNORECASE,
)

_LEFTCOL = re.compile(r"^[A-Za-z_][\w.]*$")
_PATTERN_OPS = ("LIKE", "ILIKE", "GLOB", "REGEXP", "MATCH")


def _optr_fold(match: re.Match) -> str:
    left, op, right = match.group("left"), match.group("op"), match.group("right")
    norm = " ".join(op.split()).upper()
    if any(p in norm for p in _PATTERN_OPS) or norm in ("IN", "NOT IN"):
        # Pattern / containment: fold only the marked (right) operand.
        return f"{left} {op} LOWER({right})"
    # Comparisons (incl. AND/OR prefixes): fold BOTH operands so the
    # case-insensitivity SQLite's COLLATE provided survives.
    return f"LOWER({left}) {op} LOWER({right})"


_ORDER_RE = re.compile(
    r"(\bORDER\s+BY\s+(?:[^,()]*?,\s*)?)(?P<col>[A-Za-z_][\w.]*)"
    r"\s+COLLATE\s+NOCASE(?P<dir>\s+(?:ASC|DESC))?",
    re.IGNORECASE,
)

# Reverse comparator:  col COLLATE NOCASE = expr   (COLLATE hangs off
# the LEFT operand). Normalize to the forward shape so _OPTR folds
# both sides on the next pass.
_REV_RE = re.compile(
    r"(?P<left>" + _OPERAND + r")"
    r"\s+COLLATE\s+NOCASE\s*(?P<op>" + _OP + r")\s*(?P<right>" + _OPERAND + r")",
    re.IGNORECASE,
)

_BARE_RE = re.compile(
    r"(?P<op>" + _OPERAND + r")\s+COLLATE\s+NOCASE\b",
    re.IGNORECASE,
)

_IS_NOT_LITERAL_RE = re.compile(
    r"(\b" + _OPERAND + r")\s+IS\s+NOT\s+(?!\bNULL\b|NOT\s+\bNULL\b)"
    r"((?:" + _OPERAND + r"))",
    re.IGNORECASE,
)


_MINMAX2 = re.compile(
    # SQLite scalar min(a,b)/max(a,b) with exactly two args (no nested
    # parens inside: the core's uses are column pairs like
    # min(fact_a_id, fact_b_id) in the E8 conflicts pair index).
    r"\b(min|max)\s*\(\s*([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)\s*,\s*"
    r"([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)\s*\)",
    re.IGNORECASE)


def _minmax2(m: re.Match) -> str:
    return ("least(" if m.group(1).lower() == "min" else "greatest(") + \
           f"{m.group(2)}, {m.group(3)})"


def rewrite_dialect(sql: str) -> str:
    # SQLite 2-arg scalar min()/max() has no PG twin (PG min/max are
    # aggregates only): rewrite to least()/greatest(). Without this the
    # vendored E8 migration (idx_conflicts_pair_norm on
    # min(fact_a_id, fact_b_id)) dies with 'No function matches the given
    # name and argument types' on first init of a fresh PG schema.
    sql = _MINMAX2.sub(_minmax2, sql)
    if "COLLATE" not in sql.upper() and "IS NOT" not in sql.upper():
        return sql
    out = sql
    if "COLLATE" in out.upper():
        prev = None
        while prev != out:
            prev = out
            out = _ORDER_RE.sub(
                lambda m: f"{m.group(1)}lower({m.group('col')}){m.group('dir') or ''}",
                out)
            out = _REV_RE.sub(
                lambda m: f"{m.group('left')} {m.group('op')} {m.group('right')} COLLATE NOCASE",
                out)
            out = _OPTR.sub(_optr_fold, out)
            out = _BARE_RE.sub(r"LOWER(\g<op>)", out)
    if "IS NOT" in out.upper():
        out = _IS_NOT_LITERAL_RE.sub(r"\1 IS DISTINCT FROM \2", out)
    return out
