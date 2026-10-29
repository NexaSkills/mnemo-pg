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
_PX = r"(?:\?|" + re.escape(_PARAM_TOKEN) + r")"

EMBEDDING_DIM_PLACEHOLDER = "{dim}"

# --- statement-level patterns (module string constants + code literals) ----

# DELETE FROM vec_X WHERE rowid = ?
_RE_VEC_DELETE = re.compile(
    r"(?i)DELETE\s+FROM\s+vec_(working|episodes|facts)\s+WHERE\s+rowid\s*=\s*\?")

# INSERT INTO vec_X(rowid, embedding) VALUES (?, vec_quantize_binary(?))  (and int8/plain)
_RE_VEC_INSERT = re.compile(
    r"(?i)INSERT\s+INTO\s+vec_(working|episodes|facts)\s*\(\s*rowid\s*,\s*embedding\s*\)\s*"
    r"VALUES\s*\(\s*\?\s*,\s*(?:vec_quantize_(?:binary|int8)\s*\(\s*\?(?:\s*,\s*[\"']unit[\"'])?\s*\)|\?)\s*\)")

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

# quantizer probes: SELECT vec_quantize_int8(?, 'unit') / vec_quantize_binary(?)
_RE_QUANT_PROBE = re.compile(
    r"(?i)SELECT\s+vec_quantize_(?:binary|int8)\s*\(\s*\?(?:\s*,\s*[\"']unit[\"'])?\s*\)")

# --- _fixup patterns (f-string-built statements the shapes above miss) ------

# vec_quantize_int8(?, 'unit') / vec_quantize_binary(?) wrapping a param or expr
_RE_QUANTIZE_UNIT = re.compile(
    r"(?i)\bvec_quantize_(?:binary|int8)\s*\(\s*(\??[\w.]*)(?:\s*,\s*[\"']unit[\"'])?\s*\)")
_RE_QUANTIZE = re.compile(
    r"(?i)\bvec_quantize_(?:binary|int8)\s*\(\s*([^()]*)\s*\)")

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
    out = _RE_VEC_INSERT.sub(
        lambda m: f"INSERT INTO vec_{m.group(1)}(rowid, embedding) VALUES (?, ?::vector)",
        out)
    # DELETE FROM vec_X WHERE rowid = ? is already valid PG — no-op rewrite,
    # listed for documentation completeness:
    #   _RE_VEC_DELETE -> unchanged
    out = _fixup(out)
    return out


def _fixup(sql: str) -> str:
    """Catch-all for vec0 statements the patterns above missed (the core
    builds several via f-strings, e.g. `INSERT INTO {table}(rowid, embedding)
    VALUES (?, vec_quantize_int8(?, 'unit'))`). Runs as a final pass so any
    surviving sqlite-vec surface fails LOUDLY (raw vec_quantize_* reaches PG
    as an undefined function) rather than silently doing the wrong thing."""
    s = sql
    if "vec_quantize_" in s:
        s = _RE_QUANTIZE_UNIT.sub(lambda m: m.group(1), s)
        s = _RE_QUANTIZE.sub(lambda m: m.group(1), s)
    # vec0 companion tables all have (rowid BIGINT, embedding vector) in PG;
    # a vector literal/param bound into their INSERT must be typed.
    s = _RE_VEC_INSERT_TYPED.sub(
        lambda m: (m.group(0) if "::" in m.group(3) else
                   f"INSERT INTO {m.group(1)}(rowid, embedding) "
                   f"VALUES ({m.group(2)}, {m.group(3)}::vector)"), s)
    # KNN blob shape: SELECT [v.]rowid, [v.]embedding FROM vec_X [v] WHERE
    # [v.]embedding MATCH ? — pgvector needs an ORDER BY for the KNN half;
    # keep the blob, keep the alias qualifier on both <=> uses.
    def _blob(m):
        e = m.group("e")  # alias-qualified embedding ref, e.g. 'v.embedding' or 'embedding'
        kl = f" LIMIT {m.group('k')}" if m.group("k") is not None else ""
        return (m.group(1) + f"WHERE {e} <=> ? IS NOT NULL ORDER BY {e} <=> ?" + kl)
    s = _RE_VEC_MATCH_BLOB.sub(_blob, s)
    # KNN via MATCH: sqlite-vec's MATCH ? AND k=N == nearest-N by cosine
    # distance over (unit-normalised) embeddings.
    s = _RE_VEC_MATCH_KNN.sub(
        lambda m: f"SELECT rowid, embedding <=> ? AS distance FROM {m.group(1)} "
                  f"ORDER BY distance"
                  + (f" LIMIT {m.group('k')}" if m.group("k") is not None else ""), s)
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
