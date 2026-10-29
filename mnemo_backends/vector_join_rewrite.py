"""Runtime net: rewrite the core's aliased vec KNN JOIN shape.

Core shape (built via f-string inside _wm_vec_search_sqlite, so it is not in
any module literal — rewrite_module misses it and the runtime net catches it):

  SELECT wm.id, vw.distance[, vw.embedding]
  FROM vec_working vw
  JOIN <t> wm ON <wm>.rowid = vw.rowid
  WHERE vw.embedding MATCH <expr>
    AND k = ?
    AND <where_sql>
  ORDER BY vw.distance

Binds: (emb_json, k, *where_params). k sits in the MIDDLE of the param list,
so it is pulled out: sqlite-vec KNN is nearest-N by cosine distance, which in
pgvector is exactly an ordered subquery with LIMIT k. Bind order is kept:
params[0] feeds the vector literal, params[1] (k) feeds LIMIT via %s::int,
and where_params stay as trailing %s.

The content-table side of the join keeps <alias>.rowid: the downstream rowid
rewriter maps alias.rowid -> the shim column, exactly as before.
"""
import re

_VEC_TABLES = {"vec_working", "vec_episodes", "vec_facts"}

# `?` or the fts_conn sentinel (the FTS cursor protects binds before the
# vector hooks run; see vector_rewrite._PARAM_TOKEN).
try:
    from .fts_conn import _SENTINEL as _PARAM_TOKEN
except Exception:
    _PARAM_TOKEN = "\x00\x01\x00"
_PX = r"(?:\?|" + re.escape(_PARAM_TOKEN) + r")"

_RE_ALIASED_KNN_JOIN = re.compile(
    r"(?is)\bSELECT\s+(?P<jalias>\w+)\.id\s*,\s*(?P<valias>\w+)\.distance"
    r"(?P<blob>\s*,\s*\w+\.embedding)?\s+"
    r"FROM\s+(?P<vec>vec_\w+)\s+(?P<valias2>\w+)\s+"
    r"JOIN\s+(?P<jt>\w+)\s+(?P<jalias2>\w+)\s+ON\s+(?P<jcond>.+?)\s+"
    r"WHERE\s+\w+\.embedding\s+MATCH\s+"
    r"(?:vec_quantize_(?:binary|int8)\s*\([^)]*\)|" + _PX + r")"
    r"\s+AND\s+k\s*=\s*" + _PX +
    r"(?P<rest>(?:\s+AND\s+|\s+)[^\n]*?)"
    r"(?P<order>\s+ORDER\s+BY\s+\w+\.distance)?\s*$"
)


def rewrite_aliased_knn(sql: str, params):
    """Returns (sql, params). Unchanged when the shape does not match."""
    if ("k = ?" not in sql and "k=?" not in sql
            and "k = " + _PARAM_TOKEN not in sql
            and "k=" + _PARAM_TOKEN not in sql):
        return sql, params
    m = _RE_ALIASED_KNN_JOIN.search(sql)
    if not m:
        return sql, params
    if not (isinstance(params, (tuple, list)) and len(params) >= 2
            and isinstance(params[1], (int, float))):
        # binds don't look like (vec, k, *where): do not guess. The core
        # passes an f-string k (a str param) — that is still the
        # (vec, k, *where) contract, so accept numeric strings too.
        if not (isinstance(params, (tuple, list)) and len(params) >= 2
                and isinstance(params[0], (str, bytes))
                and isinstance(params[1], str)
                and params[1].strip().isdigit()):
            return sql, params
    vec_t = m.group("vec")
    jalias = m.group("jalias")
    if m.group("jalias2") != jalias or m.group("valias2") != m.group("valias"):
        return sql, params
    jt = m.group("jt")
    if m.group("blob"):
        # the outer query asked for the vector column too: the _knn subquery
        # projects it and the outer list reads it as `_knn.embedding` under
        # its original name (PG cannot resolve <valias>.embedding after the
        # vec table disappears from FROM; core's own key() lookup only needs
        # the column *named* 'embedding' in the result row).
        knn_cols = "rowid, embedding"
        blob = ""
    else:
        knn_cols = "rowid"
        blob = ""
    jcond = re.sub(r"\b%s\s*\.\s*rowid\b" % re.escape(m.group("valias")),
                   "_knn.rowid", m.group("jcond"))
    rest = (m.group("rest") or "").strip()
    rl = rest.lower()
    if rl.startswith("and "):
        rest_where = rest[4:]
    elif rl.startswith("where "):
        rest_where = rest[6:]
    else:
        rest_where = rest
    # vec companion tables have a REAL rowid column in PG; spell the inner
    # rowid table-qualified so the downstream bare-rowid rewriter (flat alias
    # map, subquery scoping invisible) leaves it alone instead of grabbing it
    # for the JOIN table's shim column.
    inner = "%s.rowid" % vec_t if vec_t in _VEC_TABLES else "rowid"
    inner_cols = inner + (", %s.embedding" % vec_t if m.group("blob") else "")
    outer_blob = ", _knn.embedding" if m.group("blob") else ""
    out = (
        "SELECT %s.id, _knn.distance%s "
        # inner rowid spelled table-qualified where the vec table has a REAL
        # rowid column: the downstream bare-rowid rewriter sees a flat alias
        # map (subquery scoping invisible) and would otherwise grab this bare
        # rowid for the JOIN table's shim column. Qualified real-rowid refs
        # are left untouched by that rewriter.
        "FROM (SELECT %s, embedding <=> ? AS distance "
        "FROM %s ORDER BY distance LIMIT ?::int) _knn "
        "JOIN %s %s ON %s"
        "%s ORDER BY _knn.distance"
        % (jalias, outer_blob, inner_cols, vec_t, jt, jalias, jcond,
           (" WHERE " + rest_where) if rest_where.strip() else "")
    )
    return out, tuple(params)
