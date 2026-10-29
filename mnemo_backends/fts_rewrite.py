"""FTS5 MATCH rewriting for the native PG backend.

The vendored core emits SQLite FTS5 shapes against the ``fts_<base>``
shadow views provisioned natively:

    SELECT <id>, rank FROM fts_x
     WHERE [fts_x | <view>.<col> | <col>] MATCH <operand> [AND ...]
     ORDER BY rank [, <id>] LIMIT ?

Postgres runs the native equivalent against the base table's tsvector
column:

    SELECT <id>, -ts_rank(<tsv>, q.tsq) AS rank
      FROM <base> CROSS JOIN LATERAL
           (to_tsquery('simple', mnemo_normalize_fts(?))) AS q(tsq)
     WHERE <tsv> @@ q.tsq [AND ...]
     ORDER BY rank [, <id>] LIMIT ?

The LATERAL carries the query placeholder exactly ONCE, so the rewritten
statement binds the same positional parameters the core passed for the
original statement (query, then LIMIT) — placeholders are never
duplicated or reordered. ``rank`` is negated ``ts_rank`` so SQLite bm25
ordering (smaller is better) survives as PG ascending-rank ordering.
FTS5 prefix terms (``tok*``) map to tsquery prefix matches (``tok:*``)
through ``mnemo_normalize_fts``. Only statements whose MATCH sites ALL
target a known FTS-indexed table are rewritten; anything else passes
through unchanged so failures stay loud.
"""

from __future__ import annotations

import re

# base table -> {fts column: tsvector column serving it}
_INDEXED = {
    "working_memory": {"content": "content_tsv"},
    "episodic_memory": {"content": "content_tsv", "summary": "summary_tsv"},
    "facts": {"subject": "object_tsv", "predicate": "object_tsv",
              "object": "object_tsv"},
}
# exact identifiers naming these tables: the base, its fts_<base> shadow
# view, and the fts5 DDL short names (fts_working / fts_episodes /
# fts_facts) the core uses as table names. Membership is an exact-name
# map — never string slicing.
_ALIASES = {b: b for b in _INDEXED}
for _b in _INDEXED:
    _ALIASES["fts_" + _b] = _b
_ALIASES.update({"fts_working": "working_memory",
                 "fts_episodes": "episodic_memory",
                 "fts_facts": "facts"})

# operand token: a ? placeholder, a 'single-quoted', or a "double-quoted"
_OPERAND = r"(\?|'(?:[^']|'')*'|\"[^\"]*\")"
# <ident> [ . <ident> ] MATCH <operand> — g1 head, g2 qualifier, g3 operand
_MATCH_RE = re.compile(
    r"\b(\w+)(?:\s*\.\s*(\w+))?\s+MATCH\s+" + _OPERAND, re.I)
# FROM relation identifier (whole word; \w+ swallows a leading fts_)
_FROM_RE = re.compile(r"\bFROM\s+(?:\w+\s*\.\s*)?(\w+)", re.I)


def _base_name(name: str):
    """Map an identifier to its base table name via the alias map, or None."""
    return _ALIASES.get(name.lower())


def _tsq_expr(operand: str) -> str:
    """Operand SQL ('literal' / "quoted" / ?) -> tsquery expression."""
    if operand.startswith("?"):
        return "to_tsquery('simple', mnemo_normalize_fts(%s))"
    if operand.startswith("'"):
        inner = operand[1:-1].replace("''", "'")
    else:  # "quoted string" literal
        inner = operand[1:-1].replace('""', '"')
    return ("to_tsquery('simple', mnemo_normalize_fts("
            + "'" + inner.replace("'", "''") + "'))")


def rewrite_fts_sql(sql: str):
    """Return (new_sql, changed: bool)."""
    if "MATCH" not in sql.upper():
        return sql, False
    froms = list(_FROM_RE.finditer(sql))
    bases = [_base_name(m.group(1)) for m in froms]
    if not any(bases):
        return sql, False
    base = next(b for b in bases if b)
    cols = _INDEXED[base]
    matches = list(_MATCH_RE.finditer(sql))
    if not matches:
        return sql, False
    own = {k for k, v in _ALIASES.items() if v == base}
    # a MATCH head naming a DIFFERENT known table/view cannot be
    # attributed to this base's tsvector: refuse (fail loudly)
    for m in matches:
        head = (m.group(1) or "").lower()
        if head and head not in own and head in _ALIASES:
            return sql, False
    whole_row_col = "object" if base == "facts" else "content"
    # predicate rewrite: every MATCH site -> <tsv> @@ <tsq-expression>;
    # track whether any site is bound (operand == ?) so the LATERAL
    # carries exactly one placeholder.
    out = sql
    bound = False
    for m in reversed(matches):
        name, qual, operand = m.group(1), m.group(2), m.group(3)
        if qual is not None:
            col = qual
        elif name.lower() in own:
            col = whole_row_col
        else:
            col = name
        col = col.lower()
        if _ALIASES.get(col) == base and col not in _INDEXED:
            col = whole_row_col                  # view name as a column
        if col not in cols:
            return sql, False
        tsv = cols[col]
        qexpr = _tsq_expr(operand)
        bound = bound or operand == "?"
        repl = "%s @@ %s" % (tsv, qexpr)
        out = out[: m.start()] + repl + out[m.end():]
    # FROM <view> -> FROM <base>
    for v, b in _ALIASES.items():
        if v != b:
            out = re.sub(r"\b(FROM\s+(?:\w+\s*\.\s*)?)" + v + r"\b",
                         r"\1" + b, out, flags=re.I)
    if not bound:
        return out, True
    # bound statement: move the tsquery into a CROSS JOIN LATERAL so the
    # one %s is bound once; the predicate and rank projection reference
    # q.tsq. Rebuild the FROM clause and add the rank projection when the
    # core ordered by it.
    return _lateralise(out, base), True


def _lateralise(sql: str, base: str) -> str:
    """Rewrite a bound FTS statement to the lateral-tsquery shape."""
    select_m = re.search(r"\bSELECT\b", sql, re.I)
    from_m = re.search(r"\bFROM\s+(?:\w+\s*\.\s*)?\w+", sql, re.I)
    w = re.search(r"\bWHERE\b", sql[from_m.end():], re.I)
    where_at = from_m.end() + w.start() if w else None
    cols = sql[select_m.end(): from_m.start()].strip().rstrip(",")
    # the core selects "rank" as a bare column from the fts view; that
    # projection is exactly the ts_rank expression we add, so drop the
    # bare "rank" entry from the list (else it shadows our alias).
    parts = [q.strip() for q in cols.split(",")] if cols else []
    kept = [q for q in parts if q and q.lower() not in
            ("rank", base + ".rank")]
    # if rank was the ONLY projection (SELECT rank FROM ...), keep the
    # column list empty and rely on the ts_rank alias below.
    cols = ", ".join(kept)
    relation = sql[from_m.start(): where_at if where_at is not None else len(sql)]
    tail = sql[where_at:] if where_at is not None else ""
    # the predicate's inline BOUND tsquery text (to_tsquery of the ?
    # placeholder) becomes q.tsq; a literal-only tsquery (already inlined
    # by _tsq_expr) never uses %s and never triggers the lateral path.
    qexpr_m = re.search(
        r"@@\s*(to_tsquery\('simple',\s*mnemo_normalize_fts\(%s\)\))",
        tail, re.I)
    if qexpr_m is None:
        return sql
    tail = tail.replace(qexpr_m.group(1), "@@ q.tsq", 1) if False else \
        tail[: qexpr_m.start(1)] + "q.tsq" + tail[qexpr_m.end(1):]
    proj = ""
    if re.search(r"\bORDER\s+BY\s+rank\b", sql, re.I) and "AS rank" not in sql.lower():
        t = re.search(r"\b(\w+_tsv)\s+@@", sql, re.I)
        if t:
            proj = ", -ts_rank(%s, q.tsq) AS rank" % t.group(1)
    head = sql[: select_m.end()]
    proj_list = [q for q in ([cols] if cols else []) if q]
    if proj:
        proj_list.append(proj.lstrip(", "))
    if not proj_list:
        proj_list.append("true AS _mnemo_fts_ok")
    return (head + " " + ", ".join(proj_list) + " "
            + relation + " CROSS JOIN LATERAL (SELECT " + qexpr_m.group(1)
            + " AS tsq) AS q" + (" " + tail.lstrip() if tail else ""))
