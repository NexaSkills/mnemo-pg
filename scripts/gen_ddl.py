#!/usr/bin/env python3
"""Generate mnemo_backends/schema_ddl.py from the vendored core's DDL.

Approach (ponytail: extract, don't hand-write): import every vendored module
that carries CREATE TABLE statements, run them against a throwaway in-memory
SQLite through a capturing shim so the core's OWN code emits the statements,
then machine-translate each CREATE into PostgreSQL. Indexes/triggers that the
core creates are replayed where expressible; FTS5/vec0 virtual tables are
replaced by the tsvector/pgvector equivalents the backends layer maintains
(fts_pg.ensure_fts / pgvector tables below).

Run from the repo root:  python3 scripts/gen_ddl.py
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))

CAPTURED: list[tuple[str, str]] = []  # (kind, sql)


class CapturingCursor:
    def __init__(self, real):
        self._real = real

    def execute(self, sql, *a, **k):
        s = sql.strip() if isinstance(sql, str) else ""
        if re.match(r"(?i)^(create|alter)\b", s):
            CAPTURED.append(("ddl", s))
        try:
            return self._real.execute(sql, *a, **k)
        except Exception:
            class _Null:
                def fetchone(self_): return None
                def fetchall(self_): return []
                def fetchmany(self_, n=1): return []
                def __iter__(self_): return iter(())
            return _Null()

    def __getattr__(self, n):
        return getattr(self._real, n)


class CapturingConn:
    def __init__(self, real):
        self._real = real

    def cursor(self, *a, **k):
        return CapturingCursor(self._real.cursor())

    def execute(self, sql, *a, **k):
        return self.cursor().execute(sql, *a, **k)

    def __getattr__(self, n):
        return getattr(self._real, n)


def capture():
    import sqlite3 as _s3

    _real_connect = _s3.connect

    class _NullResult:
        __slots__ = ()
        def fetchone(self): return None
        def fetchall(self): return []
        def fetchmany(self, n=1): return []
        def __iter__(self): return iter(())
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _CapturingCursor:
        def __init__(self, real):
            self._real = real
        def execute(self, sql, *a, **k):
            self._cap(sql)
            try:
                self._real.execute(sql, *a, **k)
            except Exception:
                pass
            return _NullResult()
        def executemany(self, sql, seq):
            self._cap(sql)
            try:
                self._real.executemany(sql, list(seq)[:1])
            except Exception:
                pass
            return _NullResult()
        def executescript(self, script):
            if isinstance(script, str):
                for stmt in re.split(r";\s*(?:\n|$)", script):
                    self._cap(stmt)
            try:
                self._real.executescript(script)
            except Exception:
                pass
            return _NullResult()
        def _cap(self, sql):
            if isinstance(sql, str):
                for piece in (" " + " ".join(sql.split()),):
                    if re.match(r"(?i)^\s*(create|alter)\b", piece):
                        CAPTURED.append(("ddl", piece.strip()))
        def __getattr__(self, n):
            return getattr(self._real, n)

    class _CapturingConn:
        def __init__(self, real):
            self._real = real
        def cursor(self, *a, **k):
            return _CapturingCursor(self._real.cursor())
        def execute(self, sql, *a, **k):
            return self.cursor().execute(sql, *a, **k)
        def executemany(self, sql, seq):
            return self.cursor().executemany(sql, seq)
        def executescript(self, s):
            return self.cursor().executescript(s)
        def __getattr__(self, n):
            return getattr(self._real, n)

    _s3.connect = lambda *a, **k: _CapturingConn(_real_connect(":memory:"))
    # import every core module with DDL
    for modname in (
        "mnemosyne.core.beam", "mnemosyne.core.memory", "mnemosyne.core.canonical",
        "mnemosyne.core.episodic_graph", "mnemosyne.core.annotations",
        "mnemosyne.core.cost_log", "mnemosyne.core.hygiene", "mnemosyne.core.media",
        "mnemosyne.core.query_cache", "mnemosyne.core.sync", "mnemosyne.core.journal",
        "mnemosyne.core.triples", "mnemosyne.core.banks", "mnemosyne.core.binary_vectors",
    ):
        try:
            importlib.import_module(modname)
        except Exception as e:
            print(f"import fail {modname}: {e}", file=sys.stderr)
    # force the real full schema build: instantiate a BeamMemory
    try:
        from mnemosyne.core.beam import BeamMemory
        BeamMemory(session_id="ddlgen",
                   db_path=pathlib.Path("/tmp/mnemo_ddlgen/mnemosyne.db"))
    except Exception as e:
        print(f"beam init note: {e}", file=sys.stderr)
    return


# --- sqlite DDL -> PG translation --------------------------------------------

TYPE_MAP = [
    (re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.I), "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"),
    (re.compile(r"\bINTEGER\b", re.I), "BIGINT"),
    (re.compile(r"\bREAL\b", re.I), "DOUBLE PRECISION"),
    (re.compile(r"\bBLOB\b", re.I), "BYTEA"),
    (re.compile(r"\bTEXT\b", re.I), "TEXT"),  # identity, keeps the map explicit
]

_VT_SKIP = re.compile(r"(?i)create\s+virtual\s+table")


def translate_create(sql: str) -> str | None:
    s = " " + " ".join(sql.split()) + " "
    if _VT_SKIP.search(s):
        return None  # FTS5/vec0 handled by fts_pg + pgvector bootstrap
    s = re.sub(r"(?i)^\s*create\s+virtual\s+table", "CREATE TABLE", s)
    s = re.sub(r"(?i)\s+using\s+fts5\s*\(", " (", s)  # fts5 standalone tables
    s = re.sub(r"(?i)\s+with\s*\([^)]*\)", " ", s)
    s = re.sub(r"(?i)create\s+index\s+(if\s+not\s+exists\s+)?([\w$]+)\s+on\s+", r"CREATE INDEX \1\2 ON ", s)
    if re.search(r"(?i)unique\s*\(", s) and re.search(r"(?i)create\s+index", s):
        s = re.sub(r"(?i)create\s+index", "CREATE UNIQUE INDEX", s, count=1) if "UNIQUE" in s.upper() and False else s
    if re.search(r"(?i)stridx", s):
        return None
    s = re.sub(r"(?i)\s+stridx\s*=\s*[\w.]+", "", s)
    if not re.search(r"(?i)^create\s+(table|(unique\s+)?index)\b", s.strip()):
        return None
    if re.search(r"(?i)^\s*create\s+(unique\s+)?index", s.strip()):
        # indexes are collected before column/type mangling below
        m = re.match(r"(?i)^(CREATE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:UNIQUE\s+)?INDEX\s+)(?!IF\s+NOT\s+EXISTS)([\w.\"]+)", s.strip())
        if m:
            s = m.group(1) + "IF NOT EXISTS " + m.group(2) + s.strip()[len(m.group(0)):]
        s = re.sub(r"(?i)\bON\s+\"?main\"?\.", "ON ", s)
        return s.strip()
    # column-level DEFAULT (strftime(...)) etc inside create: translate forms
    s = re.sub(r"\bstrftime\s*\(\s*'%s'\s*,\s*'now'\s*\)", "extract(epoch from now())", s, flags=re.I)
    s = re.sub(r"\bDEFAULT\s+\"?now\"?\(\)", "DEFAULT now()", s, flags=re.I)
    # apply type map only inside the paren body (avoid mangling table names)
    m = re.match(r"(?i)^(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\w.\"]+\s*\()(.*)(\)\s*\)?)$", s.strip(), re.S)
    if not m:
        # table without parens body match -> try naive
        body_head, body, tail = "", s, ""
        for pat, rep in TYPE_MAP:
            s = pat.sub(rep, s)
        return s.strip()
    head, body, tail = m.groups()
    for pat, rep in TYPE_MAP:
        body = pat.sub(rep, body)
    return (head + body + tail).strip()


def main():
    capture()
    out = []
    seen_tables: dict[str, str] = {}
    indexes = []
    for kind, sql in CAPTURED:
        t = translate_create(sql)
        if not t:
            continue
        tm = re.match(r"(?i)^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w\"]+)", t)
        if tm:
            name = tm.group(1).strip('"').lower()
            if name in ("fts_working", "fts_episodes", "fts_facts",
                        "vec_working", "vec_episodes", "vec_facts"):
                continue  # replaced by tsvector cols + pgvector tables
            if name in seen_tables:
                continue
            seen_tables[name] = t
        elif re.match(r"(?i)^CREATE\s+(UNIQUE\s+)?INDEX", t):
            indexes.append(t)
    lines = [
        "# mnemo-pg DDL — generated by scripts/gen_ddl.py from the vendored",
        "# mnemosyne core's own CREATE TABLE statements (sqlite shapes),",
        "# machine-converted to PostgreSQL. DO NOT EDIT BY HAND: regenerate",
        "# (python3 scripts/gen_ddl.py) and review the diff.",
        "",
        "DDL: list[str] = [",
    ]
    for name in sorted(seen_tables):
        stmt = seen_tables[name]
        if "IF NOT EXISTS" not in stmt.upper():
            stmt = stmt.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
        lines.append("    " + repr(_qualify(stmt)) + ",")
    lines.append("]")
    lines.append("")
    lines.append("INDEXES: list[str] = [")
    for t in indexes:
        if re.search(r"(?i)fts_|_fts|vec_", t):
            continue
        stmt = re.sub(r"(?i)^CREATE\s+(UNIQUE\s+)?INDEX\s+(?!IF\s+NOT\s+EXISTS)", lambda m: m.group(0) + "IF NOT EXISTS ", t, count=1)
        stmt = re.sub(r"\bON\s+\"?main\"?\.", "ON ", stmt)
        lines.append("    " + repr(_qualify(stmt)) + ",")
    lines.append("]")
    (ROOT / "mnemo_backends" / "schema_ddl.py").write_text("\n".join(lines) + "\n")
    print(f"wrote schema_ddl.py: {len(seen_tables)} tables, {len(indexes)} indexes")


def _qualify(stmt: str) -> str:
    stmt = re.sub(r"(?i)\bmain\.", "", stmt)
    return stmt


if __name__ == "__main__":
    main()
