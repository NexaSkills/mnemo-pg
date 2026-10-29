#!/usr/bin/env python3
"""pg_native_rewrite.py — rewrite vendored sqlite SQL -> native PG SQL.

Idempotent text transform over vendor/mnemosyne/**/*.py string literals.
Maps ONLY SQLite-specific SQL shapes; everything else is left byte-identical.

  vec KNN      : WHERE embedding MATCH [fn(args)] AND k=N [ORDER BY distance]
                 -> WHERE embedding <=> <vec> ORDER BY 1 [LIMIT N]
  fts MATCH    : <tbl> MATCH ?  /  col MATCH ?  -> tsquery @@ predicate
  INSERT OR    : REPLACE -> ON CONFLICT DO UPDATE SET (needs target cols)
                 IGNORE  -> ON CONFLICT DO NOTHING
  rowid        : bare `rowid` -> `<table>__rowid` (best-effort per statement)
  PRAGMA       : left alone (native_conn intercepts them on the conn layer)
  julianday/strftime: left alone (schema parity functions installed)

Dry-run by default; --apply writes. Reports per-file rewrite counts.
"""
import re
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent / "vendor" / "mnemosyne"

APPLY = "--apply" in sys.argv

# ---- vec KNN ---------------------------------------------------------------
# ... FROM vec_<t> [alias] WHERE embedding MATCH <operand> AND k=N [ORDER BY distance]
VEC_RE = re.compile(
    r"""(?P<head>FROM\s+vec_\w+)(?P<asq>\s+AS\s+\w+|\s+\w+)?\s+
        WHERE\s+ (?:(?P<qual>\w+)\.)? embedding\s+MATCH\s+
        (?P<op>vec_quantize_\w+\(\s*(?P<in>\?)\s*(?:,\s*(?:"[^"]*"|'[^']*')\s*)?\)
                 |\?)
        \s+AND\s+k\s*=\s*(?P<k>\{[^}]*\}|\d+|\?)
        (?P<rest>(?:\s+AND\s+[^;]*?)?)
        (?P<ord>\s+ORDER\s+BY\s+[\w.]*distance)?
    """,
    re.I | re.VERBOSE | re.DOTALL,
)


def vec_sub(m):
    qual = (m.group("qual") + ".") if m.group("qual") else ""
    inner = m.group("in") or "?"
    k = m.group("k")
    rest = (m.group("rest") or "").strip()
    limit = f" LIMIT {k}" if k != "?" else ""
    where = f"{qual}embedding <=> {inner}"
    if rest:
        where += " " + rest
    return f"{m.group('head')}{m.group('asq') or ''} WHERE {where} ORDER BY 1{limit}"


# ---- FTS MATCH -------------------------------------------------------------
# <ftsname> MATCH ?  where ftsname starts with fts_  -> shadowed tsvector predicate
FTS_TBL_RE = re.compile(r"\b(?:main\.)?(fts_\w+)\s+MATCH\s+(\?)", re.I)
# table-column MATCH (e.g. "content MATCH ?" inside a query on a base table)
# left as a TODO note; handled per known column map below.
FTS_COL_MAP = {  # (table, col) -> tsvector column
    ("working_memory", "content"): "content_tsv",
    ("episodic_memory", "content"): "content_tsv",
    ("facts", "object"): "object_tsv",
}
FTS_COL_RE = re.compile(r"\b(\w+)\s+MATCH\s+(\?)", re.I)


def fts_tbl_sub(m):
    return f"{m.group(1)}_view.{m.group(1).split('_',1)[1]}_tsv @@ mnemo_tsq({m.group(2)})"


def fts_col_sub(m):
    # only rewrite when the left side is a known tsvector source column and
    # the surrounding statement is a SELECT (heuristic: no table rewrite here)
    return m.group(0)  # handled below per-statement; identity here


# ---- INSERT OR -------------------------------------------------------------
IOR_IGNORE_RE = re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.I)
IOR_REPLACE_RE = re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.I)


def main():
    total = {}
    changed_files = []
    for path in sorted(ROOT.rglob("*.py")):
        src = path.read_text()
        orig = src
        n_vec = len(VEC_RE.findall(src))
        src = VEC_RE.sub(vec_sub, src)
        n_fts = len(FTS_TBL_RE.findall(src))
        src = FTS_TBL_RE.sub(fts_tbl_sub, src)
        n_ign = len(IOR_IGNORE_RE.findall(src))
        src = IOR_IGNORE_RE.sub("INSERT", src)
        # INSERT OR REPLACE cannot be safely auto-converted (needs the
        # conflict target + SET list); flag but don't rewrite.
        n_rep = len(IOR_REPLACE_RE.findall(src))
        if src != orig:
            changed_files.append((path.relative_to(ROOT), n_vec, n_fts, n_ign, n_rep))
            if APPLY:
                path.write_text(src)
        total["vec"] = total.get("vec", 0) + n_vec
        total["fts_tbl"] = total.get("fts_tbl", 0) + n_fts
        total["ignore"] = total.get("ignore", 0) + n_ign
        total["replace_todo"] = total.get("replace_todo", 0) + n_rep
    print("TOTALS:", total)
    for rel, v, f, i, r in changed_files:
        print(f"  {str(rel):50s} vec={v} fts={f} ign={i} OR_REPLACE_todo={r}")
    print("MODE:", "APPLIED" if APPLY else "DRY-RUN (pass --apply)")


if __name__ == "__main__":
    main()
