"""Enumerate EVERY column the vendored core declares — with full declarations.

The core touches an existing table through:
  * _add_column_if_missing(conn, "t", "c", DECL) — guarded add + STRICT
    validator (type / NOT NULL / DEFAULT must match DECL modulo the
    TEXT<->TIMESTAMP twin rule, see beam.py _column_matches).
  * CREATE TABLE IF NOT EXISTS t (<body>) — columns from a body the table
    didn't exist for yet.
  * CREATE INDEX IF NOT EXISTS ... ON t(col) — column must exist.
  * ALTER TABLE t ADD COLUMN c TYPE — column must exist.

DECL syntaxes in core code: literal strings, `for col, ddl in {..}.items()`
dicts, and `for col in [..]: ... {..}.get(col, 'TEXT')`. All three are parsed.
Declaration precedence: an explicit _add_column_if_missing guard for
(table, col) WINS over create-body/index fallbacks, because that guard is
what the validator compares against on an existing schema.

Output: dict table -> dict column -> decl string ("TEXT DEFAULT 'x'" etc.,
core's own SQLite spelling). Callers map to PG types.
"""

import re
from pathlib import Path

_IDX_RE = re.compile(r"CREATE INDEX IF NOT EXISTS \w+ ON (\w+)\(([^)]+)\)")
# _add_column_if_missing(x, "table", "col", "DECL")
_ADDLIT_RE = re.compile(
    r'_add_column_if_missing\(\s*[\w.]+\s*,\s*"(\w+)"\s*,\s*"(\w+)"\s*,\s*"([^"]+)"')
# cursor.execute("""...ALTER TABLE x ADD COLUMN [IF NOT EXISTS] col TYPE ..."""
_ALTER_RE = re.compile(
    r'ALTER TABLE\s+(?:"?\w+"?\.)?"?(\w+)"?\s+ADD COLUMN\s+(?:IF NOT EXISTS\s+)?(\w+)\s+([A-Za-z]+)([^")\n]*)', re.I)
# CREATE TABLE body capture: stops at a line-final ")" + optional comma + string terminator
_CT_RE = re.compile(
    r'CREATE TABLE IF NOT EXISTS\s+"?(\w+)"?\s*\((.*?)^\s*\),?\s*(?="""|"\s*\)|\'\s*\))',
    re.S | re.I | re.M)
# for col, ddl in {dict}: ... _add_column_if_missing(..., "table", col, ddl)
# dict entry values may contain escaped quotes ("\'\'"): allow them.
_DICTLOOP_RE = re.compile(
    r'(\{[^{}]*\})\s*\.items\(\)\s*:\s*(?:[^\n]*\n){0,8}?\s*_add_column_if_missing\(\s*[\w.]+\s*,\s*"(\w+)"\s*,\s*\w+',
    re.S)
# for col in [list]: ... _add_column_if_missing(..., "table", col, {dict}.get(...)
_GETLOOP_RE = re.compile(
    r'for\s+\w+\s+in\s+(\[[^\]]*\]).{0,500}?_add_column_if_missing\(\s*[\w.]+\s*,\s*"(\w+)"\s*,\s*\w+\s*,\s*\{([^{}]*?)\}\.get',
    re.S)


def _core_root():
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "vendor" / "mnemosyne",
                 here / "vendor" / "mnemosyne"):
        if cand.is_dir():
            return cand
    return None


def declared_columns(core_root=None):
    """dict table -> dict column -> core declaration (SQLite spelling)."""
    core_root = core_root or _core_root()
    guards, bodies, fallbacks = {}, {}, {}
    if core_root is None:
        return {}
    for py in sorted(core_root.rglob("*.py")):
        try:
            src = py.read_text(errors="replace")
        except OSError:
            continue
        # Python-source string escapes: keep them (do NOT strip \\' — on a
        # 2026-10-31 regression the blanket strip deleted the quote halves of
        # SQL literals like DEFAULT '' so guards lost their DEFAULT clause).
        # The dict-value capture below handles \\' and doubled '' explicitly.
        # 1. literal guards — the authoritative validator decl
        for m in _ADDLIT_RE.finditer(src):
            guards.setdefault(m.group(1), {})[m.group(2)] = m.group(3).strip()
        # 2. dict-driven guards (two loop shapes). Values in these dicts are
        #    often full column DDL ("col TEXT NOT NULL DEFAULT ''") — strip the
        #    leading column-name token if present so decl parsing sees TYPE ...
        def _norm_ddl(s):
            s = (s.replace("\\'", "'").replace('""', '"')
                  .replace("''", "'").strip())
            m2 = re.match(r"^\w+\s+((?:NOT\s+)?(?:NULL\s+)?(?:TEXT|INTEGER|REAL|BLOB|TIMESTAMP)\b.*)$",
                          s, re.I)
            return m2.group(1).strip() if m2 else s
        # dict values: any literal (single/double, incl. escaped quotes)
        _DVAL_RE = re.compile(
            r"""["'](\w+)["']\s*:\s*(?:"((?:[^"\\]|\\[\s\S]|"")*)"|'((?:[^'\\]|\\[\s\S]|'')*)')""")
        for m in _DICTLOOP_RE.finditer(src):
            for dm in _DVAL_RE.finditer(m.group(1)):
                guards.setdefault(m.group(2), {}).setdefault(
                    dm.group(1), _norm_ddl(dm.group(2) if dm.group(2) is not None
                                           else dm.group(3)))
        for m in _GETLOOP_RE.finditer(src):
            types = {dm.group(1): (dm.group(2) if dm.group(2) is not None
                                   else dm.group(3))
                     for dm in _DVAL_RE.finditer(m.group(3))}
            for cm in re.finditer(r'["\'](\w+)["\']', m.group(1)):
                guards.setdefault(m.group(2), {}).setdefault(
                    cm.group(1), _norm_ddl(types.get(cm.group(1), "TEXT")))
        # 3. CREATE TABLE bodies
        for m in _CT_RE.finditer(src):
            cols = _body_columns(m.group(2))
            for c in cols:
                bodies.setdefault(m.group(1), set()).add(c)
        # 4. index columns
        for m in _IDX_RE.finditer(src):
            for col in m.group(2).split(","):
                col = re.sub(r"\s+(ASC|DESC)$", "", col.strip(), flags=re.I)
                if col.isidentifier():
                    fallbacks.setdefault(m.group(1), set()).add(col)
        # 5. bare ALTER ADD COLUMN (declaration = its type word)
        for m in _ALTER_RE.finditer(src):
            guards.setdefault(m.group(1), {}).setdefault(
                m.group(2), (m.group(3) + " " + (m.group(4) or "")).strip())
        # 6. module-level *_DDL constants (guard shape of CREATE tables)
        _ddl_constant_decls(src, guards)
    # merge: guards win, bodies next, indexes last (all TEXT).
    # update() would let a bare-TEXT body/index entry overwrite a richer
    # guard decl (device_id lost NOT NULL DEFAULT '' this way, losing the
    # shape validate_core_contract needs). Only upgrade: a bare TEXT
    # never clobbers a decl carrying NOT NULL/DEFAULT modifiers.
    out = {}
    def _put(t, col, decl):
        cur = out.setdefault(t, {}).get(col)
        if cur is None:
            out[t][col] = decl
        else:
            richer = ("not null" in decl.lower()
                      or "default" in decl.lower())
            weaker = ("not null" not in cur.lower()
                      and "default" not in cur.lower())
            if richer and weaker:
                out[t][col] = decl
    for srcmap, default in ((guards, None), (bodies, "TEXT"), (fallbacks, "TEXT")):
        for t, v in srcmap.items():
            if isinstance(v, dict):
                for k, d in v.items():
                    _put(t, k, d or "TEXT")
            else:
                for c in v:
                    _put(t, c, default)
    return out


# 6. module-level *_DDL string constants assigned from triple-quoted literals.
#    Some components (sync engine) validate their tables via such constants
#    rather than inline cursor.execute bodies; a guard ADD is what fires on an
#    existing table, so its NOT NULL/DEFAULT shape must be in our contract.
_DDL_CONST_RE = re.compile(
    r'^([A-Z0-9_]*DDL[A-Z0-9_]*)\s*=\s*(?:[rbfu]{0,2})"""(.*?)"""', re.S | re.M)


def _ddl_constant_decls(src, guards):
    for _name, body in _DDL_CONST_RE.findall(src):
        for line in body.splitlines():
            t = re.match(r"CREATE TABLE (?:IF NOT EXISTS )?\"?([a-z_][a-z0-9_]*)\"?",
                         line.strip(), re.I)
            if t:
                _body_guards(t.group(1), body, guards)
                break


def _body_guards(table, body, guards):
    """Record CREATE-body column decls as fallbacks (never override guards)."""
    depth = 0
    for line in body.splitlines():
        s = line.strip().rstrip(",")
        if depth == 0:
            if s.upper().startswith(("PRIMARY KEY", "UNIQUE(", "CHECK",
                                     "FOREIGN KEY", "CONSTRAINT")):
                depth += s.count("(") - s.count(")")
                continue
            cm = re.match(r'"?([a-z_][a-z0-9_]*)"?\s+((?:NOT\s+NULL\s+)?[A-Za-z]+(?:\s+DEFAULT\s+[^,]+)?)',
                          s, re.I)
            if cm:
                guards.setdefault(table, {}).setdefault(cm.group(1), cm.group(2).strip())
        depth += s.count("(") - s.count(")")


def _body_columns(body):
    cols = []
    depth = 0
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        if depth == 0:
            if s.upper().startswith(("PRIMARY KEY", "UNIQUE", "CHECK",
                                     "FOREIGN KEY", "CONSTRAINT")):
                continue
            cm = re.match(r'"?([a-z_][a-z0-9_]*)"?\s+[A-Za-z]', s, re.I)
            if cm:
                cols.append(cm.group(1))
        depth += s.count("(") - s.count(")")
    return cols


if __name__ == "__main__":
    import sys
    d = declared_columns()
    print(sum(len(v) for v in d.values()), "declared columns across",
          len(d), "tables", file=sys.stderr)
    for t in sorted(d):
        print(t, sorted(d[t]))
