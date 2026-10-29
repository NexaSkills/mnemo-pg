"""Provision a mnemo-pg schema natively (idempotent) and report shape counts.

Usage:
    python3 scripts/provision_schema.py <schema> [<schema> ...]
    python3 scripts/provision_schema.py --all      # every mnemosyne* schema

Layers (in one advisory-locked transaction per schema):
  1. CREATE SCHEMA + extensions (vector, pg_trgm)
  2. declarative native DDL from scripts/native_ddl.py (no translation layer):
     identity-rowid tables, tsvector columns + triggers + GIN, julianday
     parity functions, vector companions with FK cascade + HNSW.
  3. post-assert derived structures whose *bodies* may have changed since a
     previous apply (CREATE TRIGGER IF NOT EXISTS / CREATE TABLE IF NOT EXISTS
     keep data but would otherwise keep old trigger definitions): tsvector
     triggers are DROPped and re-CREATEd from the same declarative source.

Locking (lcm-pg lesson): pg_advisory_lock per schema BEFORE any DDL so
concurrent provisioners serialize instead of racing IF-NOT-EXISTS.

DSN: $MNEMO_PG_DSN, else $HERMES_HOME/.env, else the private lcm-pg cluster.
"""
from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import hashlib
import os
import pathlib
import re
import subprocess
import sys

from mnemo_backends import psycopg_site as _ps
_p = _ps.ensure_psycopg()
import psycopg

REPO = pathlib.Path(__file__).resolve().parent.parent
LOCK_NS = 7766001  # stable namespace for mnemo-pg schema locks

# (trigger, table) pairs whose bodies come from native_ddl — re-asserted.
TSVECTOR_TRIGGERS = (
    ("working_memory_content_tsv_ts_ins", "working_memory"),
    ("episodic_memory_content_tsv_ts_ins", "episodic_memory"),
    ("episodic_memory_summary_tsv_ts_ins", "episodic_memory"),
    ("facts_fact_tsv_ts_ins", "facts"),
)
FTS_TABLES = ("working_memory", "episodic_memory", "facts", "canonical_facts",
              "triples", "media_understandings", "media_assets",
              "memoria_persona", "memoria_claims", "memoria_preferences")


def dsn() -> str:
    d = os.environ.get("MNEMO_PG_DSN")
    if d:
        return d
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    p = pathlib.Path(home) / ".env"
    if p.is_file():
        for line in p.read_text().splitlines():
            if line.startswith("MNEMO_PG_DSN="):
                return line.split("=", 1)[1].strip()
    run = pathlib.Path.home() / "lcm-pg" / "run"
    if run.is_dir():
        return f"host={run} port=54329 dbname=lcm user=lcm01"
    raise SystemExit("no MNEMO_PG_DSN and no lcm-pg/run socket dir")


def lock_key(schema: str) -> int:
    h = hashlib.sha256(f"mnemo:{schema}".encode()).digest()
    return int.from_bytes(h[:8], "big") & 0x7FFFFFFFFFFFFFFF


def ddl_for(schema: str) -> str:
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "native_ddl.py"), schema],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def _split_statements(script: str) -> list[str]:
    """Split on ';' keeping dollar-quoted bodies and string literals intact."""
    stmts: list[str] = []
    buf: list[str] = []
    i, in_dollar = 0, None
    while i < len(script):
        ch = script[i]
        if in_dollar:
            if script.startswith(in_dollar, i):
                in_dollar = None
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            m = re.match(r"\$[A-Za-z0-9_]*\$", script[i:])
            if m:
                in_dollar = m.group(0)
                buf.append(m.group(0))
                i += len(m.group(0))
                continue
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < len(script):
                if script[i] == "'":
                    buf.append("'")
                    i += 1
                    if i < len(script) and script[i] == "'":
                        buf.append("'")
                        i += 1
                        continue
                    break
                buf.append(script[i])
                i += 1
            continue
        if ch == "-" and script.startswith("--", i):
            j = script.find("\n", i)
            i = len(script) if j < 0 else j
            continue
        if ch == ";":
            s = "".join(buf).strip()
            if s:
                stmts.append(s)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    s = "".join(buf).strip()
    if s:
        stmts.append(s)
    return stmts


def provision(conn, schema: str) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
        raise SystemExit(f"unsafe schema name: {schema}")
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (lock_key(schema),))
        try:
            script = ddl_for(schema)
            try:
                with conn.transaction():
                    for stmt in _split_statements(script):
                        cur.execute(stmt)
            except psycopg.Error:
                # splitter miss: PG script mode handles any valid script
                cur.execute(script)
            # re-assert FTS trigger bodies from the same declarative source
            for trg, table in TSVECTOR_TRIGGERS:
                cur.execute(
                    'DROP TRIGGER IF EXISTS "%s" ON "%s"."%s"' % (trg, schema, table))
            for stmt in _split_statements(script):
                if stmt.lstrip().upper().startswith("CREATE TRIGGER"):
                    cur.execute(stmt)

            tables = cur.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema=%s", (schema,)).fetchone()[0]
            idx = cur.execute(
                "SELECT count(*) FROM pg_indexes WHERE schemaname=%s",
                (schema,)).fetchone()[0]
            trig = cur.execute(
                "SELECT count(DISTINCT t.tgname) FROM pg_trigger t "
                "JOIN pg_class c ON c.oid=t.tgrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND NOT t.tgisinternal", (schema,)).fetchone()[0]
            vecs = cur.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema=%s AND udt_name='vector'", (schema,)).fetchone()[0]
            gin = cur.execute(
                "SELECT count(*) FROM pg_indexes WHERE schemaname=%s "
                "AND indexdef ILIKE '%%gin%%'", (schema,)).fetchone()[0]
            hnsw = cur.execute(
                "SELECT count(*) FROM pg_indexes WHERE schemaname=%s "
                "AND indexdef ILIKE '%%hnsw%%'", (schema,)).fetchone()[0]
            fts_cols = cur.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema=%s AND udt_name='tsvector'", (schema,)).fetchone()[0]
            identity = cur.execute(
                "SELECT count(*) FROM pg_attribute a JOIN pg_class c "
                "ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND a.attidentity IN ('a','d') "
                "AND a.attnum>0", (schema,)).fetchone()[0]
            return {"schema": schema, "tables": tables, "indexes": idx,
                    "triggers": trig, "vector_cols": vecs, "gin": gin,
                    "hnsw": hnsw, "tsvector_cols": fts_cols,
                    "identity_cols": identity}
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key(schema),))


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit(__doc__)
    if argv == ["--all"]:
        conn = psycopg.connect(dsn(), autocommit=True)
        argv = [r[0] for r in conn.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE 'mnemosyne%' ORDER BY 1")]
        conn.close()
    conn = psycopg.connect(dsn(), autocommit=True)
    try:
        for schema in argv:
            info = provision(conn, schema)
            print("provisioned {schema}: {tables} tables, {indexes} indexes, "
                  "{triggers} triggers, {tsvector_cols} tsvector cols ({gin} GIN), "
                  "{vector_cols} vector cols ({hnsw} HNSW), {identity_cols} identity"
                  .format(**info))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
