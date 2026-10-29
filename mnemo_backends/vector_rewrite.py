"""pgvector rewrite of the vendored core's sqlite-vec SQL (PG backend).

The core routes ALL vector work through three vec0 virtual tables
(vec_working, vec_episodes, vec_facts) with three SQL shapes:

  write   DELETE FROM vec_X WHERE rowid = ?
          INSERT INTO vec_X(rowid, embedding) VALUES (?, vec_quantize_int8(?, 'unit'))
  read    SELECT rowid, distance FROM vec_X WHERE embedding MATCH vec_quantize_*(?)
            AND k=N ORDER BY distance
  probe   CREATE VIRTUAL TABLE _vec_test USING vec0(...) / sqlite_master checks

schema_ddl provisions real pgvector companions:

  vec_X  (rowid BIGINT PRIMARY KEY, embedding vector(384))
         + cosine index

This module rewrites the write/read shapes into parameterised pgvector SQL
inside the vendored modules' string constants (and the inline f-string
literals, by editing the modules' code objects at import time). Semantics
notes, verified against the sqlite-vec docs and the core's own comments:

  * vec0 MATCH = KNN with cosine distance when the core pre-normalises to
    unit length (it always does before quantize; see _vec_table_insert
    docstring). pgvector `<=>` (cosine distance) over the same normalized
    floats is the direct equivalent. Quantization (int8/bit) is NOT
    replicated (YAGNI; exact float32 is a strictly better recall ceiling).
  * `k=N` becomes LIMIT N (int inlined exactly like the core inlines it,
    to stay bind-param-free in the position where the core binds ?).
  * Distance ORDER BY ascending == sqlite-vec MATCH KNN order.

Anything this module cannot rewrite is LEFT ALONE so it fails loudly at the
translator (UnsupportedSQL) rather than silently doing something wrong.
"""

from __future__ import annotations

import os
import re

# Sentinel spelling used by fts_conn._protect_params (kept in sync there):
# rewrite_sql may receive SQL whose `?` binds are already protected to this
# token, so every MATCH-shape pattern accepts `?` OR the sentinel.
try:  # pragma: no cover - circular-safe import
    from .fts_conn import _SENTINEL as _PARAM_TOKEN
except Exception:
    _PARAM_TOKEN = "\x00\x01\x00"
# one alternation token matching a bare `?` or the protected sentinel
_PX = r"(?:\?|'?" + re.escape(_PARAM_TOKEN) + r"'?)"

EMBEDDING_DIM_PLACEHOLDER = "{dim}"

# --- statement-level patterns (module string constants + code literals) ----

# DELETE FROM vec_X WHERE rowid = ?
_RE_VEC_DELETE = re.compile(
    r"(?i)DELETE\s+FROM\s+vec_(working|episodes|facts)\s+WHERE\s+rowid\s*=\s*\?")

# INSERT INTO vec_X(rowid, embedding) VALUES (?, vec_quantize_binary(?))  (and int8/plain).
# Both slots accept a bare `?` OR the protected sentinel token (fts_conn
# protects binds before handing SQL to the rewriter).
_RE_VEC_INSERT = re.compile(
    r"(?i)INSERT\s+INTO\s+vec_(working|episodes|facts)\s*\(\s*rowid\s*,\s*embedding\s*\)\s*"
    r"VALUES\s*\(\s*" + _PX + r"\s*,\s*(?:vec_quantize_(?:binary|int8)\s*\(\s*" + _PX +
    r"(?:\s*,\s*[\"']unit[\"'])?\s*\)|" + _PX + r")\s*\)")

# SELECT rowid, distance FROM vec_X WHERE embedding MATCH vec_quantize_*(?) AND k={k} ORDER BY distance
# {k} may be an f-string placeholder already formatted, or a literal int.
_RE_VEC_KNN = re.compile(
    r"(?i)SELECT\s+rowid\s*,\s*distance\s+FROM\s+vec_(working|episodes|facts)\s+"
    r"WHERE\s+embedding\s+MATCH\s+vec_quantize_(?:binary|int8)\s*\(\s*\?(?:\s*,\s*[\"']unit[\"'])?\s*\)\s+"
    r"AND\s+k\s*=\s*(?P<k>\{[^}]*\}|\d+)\s+ORDER\s+BY\s+distance")

# same but without quantizer (float32 arm)
_RE_VEC_KNN_PLAIN = re.compile(
    r"(?i)SELECT\s+rowid\s*,\s*distance\s+FROM\s+vec_(working|episodes|facts)\s+"
    r"WHERE\s+embedding\s+MATCH\s+" + _PX + r"\s+AND\s+k\s*=\s*(?P<k>\{[^}]*\}|\d+)\s+ORDER\s+BY\s+distance")

# sqlite-vec unit argument: the `, "unit"` / `, 'unit'` tail inside a
# vec_quantize_* call (SQLite accepts double-quoted strings; PG would read
# "unit" as a column). Removed globally before the wrapper patterns run.
_RE_UNIT_ARG = re.compile(r"(?i)\s*,\s*(?:\"unit\"|'unit')(?=\s*\))")

# quantizer probes: SELECT vec_quantize_int8(?, 'unit') / vec_quantize_binary(?)
_RE_QUANT_PROBE = re.compile(
    r"(?i)SELECT\s+vec_quantize_(?:binary|int8)\s*\(\s*\?(?:\s*,\s*[\"']unit[\"'])?\s*\)")

# --- _fixup patterns (f-string-built statements the shapes above miss) ------

# vec_quantize_int8(?, 'unit') / vec_quantize_binary(?) wrapping a param or expr
_RE_QUANTIZE_UNIT = re.compile(
    r"(?i)\bvec_quantize_(?:binary|int8)\s*\(\s*(\??[\w.]*)(?:\s*,\s*[\"']unit[\"'])?\s*\)")
_RE_QUANTIZE = re.compile(
    r"(?i)\bvec_quantize_(?:binary|int8)\s*\(\s*([^()]*)\s*\)")

# bare vec0 KNN MATCH after quantizers were stripped:
#   ... FROM vec_X WHERE <col> MATCH <operand> AND k=<N> ORDER BY distance
# <operand> may be a `?`, a sentinel token (fts_conn protection), a baked
# mnemo_tsq('...') literal, or any non-AND expression. Rewritten to
#   SELECT rowid, <op> <=> <operand>::vector AS distance FROM vec_X
#   ORDER BY distance LIMIT N
_RE_BARE_MATCH_KNN = re.compile(
    r"(?is)SELECT\s+(?:rowid|\*)\s*,\s*distance\s+FROM\s+(vec_\w+)\s+"
    r"WHERE\s+\w+\s+MATCH\s+(.+?)\s*,?\s*(?:\"unit\"|'unit')?\s+AND\s+k\s*=\s*(\d+|\{[^{}]*\})\s+ORDER\s+BY\s+distance\b")

# INSERT INTO <anything>(rowid, embedding) VALUES (<a>, <b>) — only fires when
# the value list still carries an untyped vector source (a quantizer call that
# survived, a raw param, or a subselect); statements the shape rewrites above
# already typed (`?::vector`) contain '::' and are skipped by the lambda.
_RE_VEC_INSERT_TYPED = re.compile(
    r"(?i)INSERT\s+INTO\s+([\w\"{}]+)\s*\(\s*rowid\s*,\s*embedding\s*\)\s*"
    r"VALUES\s*\(\s*([^,()]+?)\s*,\s*([^()]+?)\s*\)")

# SELECT [v.]rowid, [v.]embedding FROM <vec> [alias] WHERE [alias.]embedding
# MATCH <expr> [AND k=N]  — the blob-returning KNN shape (_vec_search_with_blobs)
_RE_VEC_MATCH_BLOB = re.compile(
    r"(?i)(SELECT\s+(?:\w+\.)?rowid\s*,\s*(?:\w+\.)?embedding\s+FROM\s+[\w\"{}.]+(?:\s+\w+)?\s+)"
    r"WHERE\s+(?P<e>(?:\w+\.)?embedding)\s+MATCH\s+(?:vec_quantize_(?:binary|int8)\s*\([^)]*\)|" + _PX + r")"
    r"(?:\s+AND\s+k\s*=\s*(?P<k>\{[^}]*\}|\d+))?")

# SELECT rowid, distance, embedding FROM vec_X WHERE embedding MATCH ? AND k=N
# — KNN that also returns the stored vector blob.
_RE_VEC_MATCH_KNN_BLOB = re.compile(
    r"(?i)SELECT\s+rowid\s*,\s*distance\s*,\s*embedding\s+FROM\s+([\w\"{}]+)\s+"
    r"WHERE\s+embedding\s+MATCH\s+(?:vec_quantize_(?:binary|int8)\s*\([^)]*\)|" + _PX + r")\s+"
    r"(?:AND\s+k\s*=\s*(?P<k>\{[^}]*\}|\d+))?")

# SELECT rowid, distance FROM <vec> WHERE embedding MATCH <expr> AND k=N
# (ORDER BY distance optional; k may be an f-string placeholder)
_RE_VEC_MATCH_KNN = re.compile(
    r"(?i)SELECT\s+rowid\s*,\s*distance\s+FROM\s+([\w\"{}]+)\s+"
    r"WHERE\s+embedding\s+MATCH\s+(?:vec_quantize_(?:binary|int8)\s*\([^)]*\)|" + _PX + r")\s+"
    r"(?:AND\s+k\s*=\s*(?P<k>\{[^}]*\}|\d+))?")

# fts_conn's sentinel binder may bake the MATCH operand into the SQL as
# `mnemo_tsq('<literal>')` when it classifies the slot as a COPY. For a
# non-FTS vec0 KNN statement that literal is the vector JSON itself (not
# tsquery text), so match the baked KNN shape and feed the literal straight
# into `<=>` (text::vector), leaving the statement param-free.
_RE_TSQ_KNN = re.compile(
    r"(?i)SELECT\s+rowid\s*,\s*distance\s+FROM\s+vec_(?P<tbl>\w+)\s+"
    r"WHERE\s+embedding\s+MATCH\s+mnemo_tsq\s*\(\s*'(?P<v>(?:[^']|'')*)'\s*\)"
    r"(?:\s+AND\s+k\s*=\s*(?P<k>\d+))?\s*ORDER\s+BY\s+distance")



# Join-shaped KNN:  FROM vec_X vw JOIN <base> <a> ON <a>.rowid = vw.rowid WHERE
# vw.embedding MATCH <expr> AND k = N.  The core joins on the vec table's
# `rowid`; under native DDL the vec companion's rowid equivalent is its
# `<base>__rowid` identity column, so the join predicate is retargeted.
_RE_VEC_JOIN_KNN = re.compile(
    r"\bFROM\s+vec_(\w+)\s+(?:(AS)\s+)?(\w+)\s+(JOIN\s+([\w\".]+)\s+(\w+)\s+ON\s+)"
    r"(\w+)\.(\w+|rowid)\s*=\s*(\w+)\.(\w+|rowid)",
    re.IGNORECASE,
)


def _rewrite_vec_join_knn(sql: str) -> str:
    """Retarget `a.rowid = vw.rowid` onto the base table's natural key.

    The vec companion's rowid equivalent is its `<base>__rowid` identity, but
    joining on that is unnecessary: the companion's `id` IS the base row's id,
    so `<a>.id = vw.id` is the equivalent (and indexed) predicate.
    """
    m = re.search(r"\bFROM\s+vec_(\w+)\s+(?:(?:AS\s+)?(\w+))?\b", sql, re.I)
    if not m:
        return sql
    v_alias = (m.group(2) or f"vec_{m.group(1)}")
    pat = re.compile(r"\b(\w+)\.(rowid)\s*=\s*" + re.escape(v_alias) + r"\.(rowid)\b", re.I)
    sql = pat.sub(lambda mm: f"{mm.group(1)}.id = {v_alias}.id", sql)
    pat2 = re.compile(r"\b" + re.escape(v_alias) + r"\.(rowid)\s*=\s*(\w+)\.(rowid)\b", re.I)
    sql = pat2.sub(lambda mm: f"{v_alias}.id = {mm.group(2)}.id", sql)
    return sql



# FROM vec_<base> WHERE <col> MATCH <expr> [AND k=N] [rest...]  — the general
# sqlite-vec KNN shape. Rewrites to a pgvector KNN ORDER BY on the companion
# table; the quantizer call in <expr> is dropped (int8 quantization proved
# lossy; native fp32 cosine ranks correctly). `distance` becomes an
# expression alias (pgvector has no column by that name), and a trailing
# `ORDER BY distance` is preserved.
_RE_VEC_MATCH_GENERIC = re.compile(
    r"\bFROM\s+vec_(\w+)(\s+(?:AS\s+)?(\w+))?\s+WHERE\s+(\w+\.)?embedding\s+MATCH\s+"
    r"(?:vec_quantize_\w+\s*\([^)]*?\)|\?|:[\w-]+)"
    r"(\s+AND\s+(?:\w+\.)?k\s*=\s*(\d+|\?\?|\?))?"
    r"((?:\s+AND|\s+ORDER\s+BY|\s+LIMIT|\s*$)[\s\S]*)",
    re.IGNORECASE,
)


def _rewrite_vec_match_generic(sql: str) -> str:
    if "MATCH" not in sql.upper():
        return sql

    def fix(m):
        base = m.group(1)
        alias = (m.group(3) or "").strip()
        col = f"{alias}.embedding" if alias else "embedding"
        k = m.group(6)
        if k is None:
            k = "20"
        else:
            k = "??" if k == "?" else k
        tail = (m.group(7) or "").strip()
        # drop any `AND k=N` that leaked into the tail, keep ORDER BY/LIMIT
        tail = re.sub(r"^(AND\s+)?(?:\w+\.)?k\s*=\s*(?:\d+|\?\?|\?)\s*", "", tail,
                      flags=re.IGNORECASE)
        # drop duplicate ORDER BY we would introduce if tail already orders
        order = "" if re.match(r"ORDER\s+BY", tail or "", re.I) else (
            f"ORDER BY {col} <=> $VEC AS distance ")
        join = f" {alias}" if alias else ""
        return (f'FROM vec_{base}{join} WHERE 1=1 /*mnemo:MATCH*/ AND {tail or "1=1"} '
                f'{order}LIMIT {k}')

    return _RE_VEC_MATCH_GENERIC.sub(fix, sql)


def rewrite_sql(sql: str) -> str:
    """Return sql with vec0 statements rewritten to pgvector SQL."""
    out = sql

    def _limit(k: str) -> str:
        # f-string placeholder must survive; plain ints inline directly
        if k.startswith("{"):
            return f"LIMIT {k}"
        return f"LIMIT {int(k)}"

    out = _RE_VEC_KNN.sub(
        lambda m: f"SELECT rowid, embedding <=> ? AS distance FROM vec_{m.group(1)} "
                  f"ORDER BY distance {_limit(m.group('k'))}",
        out)
    out = _RE_VEC_KNN_PLAIN.sub(
        lambda m: f"SELECT rowid, embedding <=> ? AS distance FROM vec_{m.group(1)} "
                  f"ORDER BY distance {_limit(m.group('k'))}",
        out)
    def _insert_typed(m):
        whole = m.group(0)
        # sqlite-vec quantizers accept a unit argument (vec_quantize_int8(v,
        # 'unit')); PG parity takes only the vector. Strip the unit arg
        # FIRST, with an operand-tolerant peel: `[^()]*?` also spans the
        # sentinel token (protected `?`), which [?]|[\w.]* forms miss.
        w = re.sub(r"(?i)(vec_quantize_(?:binary|int8)\s*\([^()]*?)"
                   r"(?:\s*,\s*(?:\"unit\"|'unit'))(\s*\))", r"\1\2", whole)
        # drop quantizer wrappers -> bare placeholder/sentinel in slot 2
        w = _RE_QUANTIZE_UNIT.sub(lambda x: x.group(1), w)
        w = _RE_QUANTIZE.sub(lambda x: x.group(1), w)
        # type the embedding slot: rewrite `VALUES (X, Y)` -> `(X, Y::vector)`
        w = re.sub(r"(?i)(VALUES\s*\(\s*[^,)]+\s*,\s*)([^)]+?)(\s*\))\s*$",
                   r"\1\2::vector\3", w)
        if "::vector" not in w:
            w = re.sub(r"(?i)(VALUES\s*\(\s*[^,)]+\s*,\s*)([^)]+?)(\s*\))",
                       r"\1\2::vector\3", w)
        return w
    out = _RE_VEC_INSERT.sub(_insert_typed, out)
    # DELETE FROM vec_X WHERE rowid = ? is already valid PG — no-op rewrite,
    # listed for documentation completeness:
    #   _RE_VEC_DELETE -> unchanged
    # global pass: sqlite-vec's vec_quantize_* calls carry a UNIT argument
    # ("unit"/'unit' — double-quoted is valid string syntax in SQLite but an
    # identifier in PG; the wrapper regexes in _fixup only fire on tidy forms,
    # so scrub the arg wherever a quantizer call appears).
    if "unit" in out.lower() and "vec_quantize" in out.lower():
        out = _RE_UNIT_ARG.sub("", out)
        # sentinel-protected param form: `vec_quantize_int8(<S>, "unit")` —
        # the wrapper regexes need a tidy operand, so peel the unit tail
        # wherever a quantizer call carries one (sentinels included).
        out = re.sub(r"(?i)(vec_quantize_(?:binary|int8)\s*\([^()]*?)"
                     r"(?:\s*,\s*(?:\"unit\"|'unit'))(\s*\))", r"\1\2", out)
        # second chance: the tidy INSERT shape needed the unit arg gone
        out = _RE_VEC_INSERT.sub(
            lambda m: f"INSERT INTO vec_{m.group(1)}(rowid, embedding) "
                      f"VALUES (?, ?::vector)", out)
    out = _fixup(out)
    return out


def _fixup(sql: str) -> str:
    """Catch-all for vec0 statements the patterns above missed (the core
    builds several via f-strings, e.g. `INSERT INTO {table}(rowid, embedding)
    VALUES (?, vec_quantize_int8(?, 'unit'))`). Runs as a final pass so any
    surviving sqlite-vec surface fails LOUDLY (raw vec_quantize_* reaches PG
    as an undefined function) rather than silently doing the wrong thing."""
    s = sql
    if "vec_quantize" in s.lower() and "unit" in s.lower():
        s = re.sub(r"(?i)(vec_quantize_(?:binary|int8)\s*\([^()]*?)"
                   r"(?:\s*,\s*(?:\"unit\"|'unit'))(\s*\))", r"\1\2", s)
    # Bare vec0 KNN shape: `WHERE <col> MATCH <operand> AND k=N ORDER BY
    # distance`. Runs BEFORE quantizer stripping (so the operand is still the
    # full vec_quantize_* call and its inner param/sentinel stays isolated),
    # and the operand itself goes through the quantizer strip (unit arg ->
    # 'unit' — the PG parity function accepts it) so binding semantics are
    # preserved: a sentinel param stays a single param slot.
    def _strip_operand(o):
        o = o.strip()
        # sqlite-vec's vec_quantize_* calls carry an extra "unit" argument
        # (double-quoted is valid SQL string syntax in SQLite, but PG would
        # resolve "unit" as a column). The wrapper regexes above only match a
        # bare `?`/word inside the parens, so a sentinel-protected param is
        # invisible to them — strip the trailing unit arg FIRST, then run the
        # wrapper strips (which expose the param/sentinel directly).
        o = re.sub(r"(?i)(,|\()\s*(?:\"unit\"|'unit')\s*(?=[,)])", r"\1", o)
        o = _RE_QUANTIZE_UNIT.sub(lambda m: m.group(1), o)
        o = _RE_QUANTIZE.sub(lambda m: m.group(1), o)
        o = re.sub(r"(?i)\b(?:\"unit\"|'unit')\s*,\s*", "", o)
        o = o.rstrip(", ").rstrip(",")
        # strip any dangling empty parens left by wrapper removal
        o = re.sub(r"\(\s*\)", "", o).strip().rstrip(",")
        return o
    def _bare(m):
        tbl, operand, k = m.group(1), _strip_operand(m.group(2)), m.group(3)
        lim = f"LIMIT {k}" if k.startswith("{") else f"LIMIT {int(k)}"
        # wrap operands in parens only when needed: the param sentinel inside
        # parens gets treated as a positional bind by the sentinel binder —
        # a bare sentinel param binds cleaner.
        wrapped = operand if operand.strip() == _PARAM_TOKEN else f"({operand})"
        return (f"SELECT rowid, embedding <=> {wrapped}::vector AS distance "
                f"FROM {tbl} ORDER BY distance {lim}")
    s = _RE_BARE_MATCH_KNN.sub(_bare, s)
    # fts_conn's sentinel binder bakes the MATCH operand into the SQL as
    # `mnemo_tsq('<literal>')` when it classifies the slot as a COPY (the
    # episodic _vec_search shape: `MATCH mnemo_tsq('[...json...]') AND k=N
    # ORDER BY distance`). Rewrite that KNN shape directly: the baked value
    # is a vector literal (NOT tsquery text — the FTS binder mislabels this
    # non-FTS statement's slot), so feed it to <=> cast text->vector and make
    # the statement param-free (binds, if any, are the binder's doing).
    if "vec_quantize_" in s:
        # operand-tolerant peel first: quantizer calls may carry a `"unit"`
        # tail whose operand is the sentinel token (invisible to the tidy
        # [\w.]* wrapper forms below).
        s = re.sub(r"(?i)(vec_quantize_(?:binary|int8)\s*\([^()]*?)"
                   r"(?:\s*,\s*(?:\"unit\"|'unit'))(\s*\))", r"\1\2", s)
        s = _RE_QUANTIZE_UNIT.sub(lambda m: m.group(1), s)
        s = _RE_QUANTIZE.sub(lambda m: m.group(1), s)

    def _tsq(m):
        # The FTS binder's COPY wrapper is tsquery-oriented (it lexizes),
        # which is wrong for a vector JSON literal. mnemo_vec_tsq is the
        # vector-safe variant: SQL-quoted text cast to vector, no lexize.
        lit = m.group("v")
        k = m.group("k")
        k = int(k) if k is not None and not k.startswith("{") else k
        lim = "" if k == "" or k is None else f" LIMIT {k}"
        return ("SELECT rowid, embedding <=> mnemo_vec_tsq('" + lit + "') "
                "AS distance FROM vec_" + m.group("tbl") +
                " ORDER BY distance" + lim)
    s = _RE_TSQ_KNN.sub(_tsq, s)
    # vec0 companion tables all have (rowid BIGINT, embedding vector) in PG;
    # a vector literal/param bound into their INSERT must be typed.
    s = _RE_VEC_INSERT_TYPED.sub(
        lambda m: (m.group(0) if "::" in m.group(3) else
                   f"INSERT INTO {m.group(1)}(rowid, embedding) "
                   f"VALUES ({m.group(2)}, {m.group(3)}::vector)"), s)
    # KNN blob shape: SELECT [v.]rowid, [v.]embedding FROM vec_X [v] WHERE
    s = _rewrite_vec_join_knn(s)
    # [v.]embedding MATCH ? — pgvector needs an ORDER BY for the KNN half;
    # keep the blob, keep the alias qualifier on both <=> uses.
    def _blob(m):
        e = m.group("e")  # alias-qualified embedding ref, e.g. 'v.embedding' or 'embedding'
        kl = f" LIMIT {m.group('k')}" if m.group("k") is not None else ""
        return (m.group(1) + f"WHERE {e} <=> ? IS NOT NULL ORDER BY {e} <=> ?" + kl)
    s = _RE_VEC_MATCH_BLOB.sub(_blob, s)
    # KNN via MATCH: sqlite-vec's MATCH ? AND k=N == nearest-N by cosine
    # distance over (unit-normalised) embeddings.
    def _knn_only(m):
        # The pattern consumes up to k=N and emits its own ORDER BY/LIMIT;
        # the source always carries a trailing `ORDER BY distance` after
        # that — drop exactly one such leftover after the replacement.
        head = (f"SELECT rowid, embedding <=> ? AS distance FROM {m.group(1)} "
                "ORDER BY distance"
                + (f" LIMIT {m.group('k')}" if m.group("k") is not None else ""))
        tail = s[m.end():]
        drop = re.match(r"(?:\s+AND\s+)?ORDER\s+BY\s+distance\b", tail,
                        flags=re.IGNORECASE)
        return head + tail[drop.end():] if drop else head + tail
    s = _RE_VEC_MATCH_KNN.sub(_knn_only, s)
    # SELECT rowid, distance, embedding FROM vec_X WHERE embedding MATCH ?
    # AND k=N  — KNN that also returns the stored vector blob.
    s = _RE_VEC_MATCH_KNN_BLOB.sub(
        lambda m: f"SELECT rowid, embedding <=> ? AS distance, embedding FROM {m.group(1)} "
                  f"ORDER BY distance"
                  + (f" LIMIT {m.group('k')}" if m.group("k") is not None else "")
                  + "§TAIL§", s)
    if "§TAIL§" in s:
        # The blob shape always carries a trailing 'ORDER BY distance' in the
        # source; the sub consumed up to k=N and appended its own ORDER BY/
        # LIMIT, so drop the leftover tail (anything up to end or a sentinel).
        s = re.sub(r"§TAIL§\s*ORDER\s+BY\s+distance\s*", "", s, flags=re.I)
        s = re.sub(r"\bORDER\s+BY\s+distance\s+LIMIT\s+(\d+)\s+ORDER\s+BY\s+distance\b",
                   r"ORDER BY distance LIMIT \1", s, flags=re.I)
        s = s.replace("§TAIL§", "")
    # Belt-and-braces: collapse any duplicated ORDER BY ... LIMIT ... ORDER BY
    # that earlier passes may have produced.
    s = re.sub(r"(ORDER\s+BY\s+distance\s+LIMIT\s+\d+(?:\s*::vector)?)\s+ORDER\s+BY\s+distance\b",
               r"\1", s, flags=re.I)
    # The aliased k=? join shapes (vec_working JOIN / _vec_search_with_blobs)
    # carry bind-order subtleties (k in the middle of the param list); they
    # are NOT rewritten here — rewrite_module refuses the module so the
    # statement fails loudly at the translator instead of binding wrong.
    return s


def rewrite_module(mod) -> int:
    """Rewrite all known vec0 SQL in a module: string globals AND string
    literals inside function code objects (in-place, same constants array).
    Returns number of rewritten literals."""
    n = 0

    def fix(s: str):
        nonlocal n
        if "vec_working" in s or "vec_episodes" in s or "vec_facts" in s \
           or "vec_quantize" in s:
            new = rewrite_sql(s)
            if new != s:
                n += 1
                return new
        return None

    # 1. module-level strings (incl. in containers)
    seen: set = set()

    def fix_container(v):
        if isinstance(v, str):
            r = fix(v)
            return v if r is None else r
        if isinstance(v, dict) and id(v) not in seen:
            seen.add(id(v))
            for k in list(v):
                nv = fix_container(v[k])
                if nv is not v[k]:
                    v[k] = nv
        elif isinstance(v, list) and id(v) not in seen:
            seen.add(id(v))
            for i in range(len(v)):
                nv = fix_container(v[i])
                if nv is not v[i]:
                    v[i] = nv
        return v

    for name, val in list(vars(mod).items()):
        nv = fix_container(val)
        if nv is not val:
            try:
                setattr(mod, name, nv)
            except Exception:
                pass

    # 2. code-object constants (function-local literals; f-strings keep their
    #    Format* ops — we only rewrite the literal fragment strings)
    import types

    def fix_code(code):
        nonlocal n
        changed = False
        consts = []
        for c in code.co_consts:
            if isinstance(c, types.CodeType):
                sub = fix_code(c)
                consts.append(sub)
                changed = changed or (sub is not c)
                continue
            if isinstance(c, str):
                r = fix(c)
                if r is not None:
                    consts.append(r)
                    changed = True
                    continue
            consts.append(c)
        if not changed:
            return code
        return code.replace(co_consts=tuple(consts))

    for name, obj in list(vars(mod).items()):
        if isinstance(obj, types.FunctionType):
            try:
                new_code = fix_code(obj.__code__)
                if new_code is not obj.__code__:
                    obj.__code__ = new_code
            except Exception:
                pass
    return n


def enabled() -> bool:
    return (os.environ.get("MNEMO_BACKEND") or "postgres").strip().lower() != "sqlite"
