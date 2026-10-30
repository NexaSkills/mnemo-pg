"""Migrate legacy sqlite mnemosyne data into the native PG schema.

Idempotent, row-preserving copy of every user table from a sqlite file
(the pre-migration ~/.hermes/mnemosyne/data/mnemosyne.db) into
mnemosyne_<profile>. Vector/blob columns are copied as raw bytes;
vec_* companion tables are left to the provider's own backfill
(rebuild_vec_from_pg / _backfill_vec_working_from_memory_embeddings).
Sequences are aligned afterwards (retire_sequences pattern).

Usage: migrate_sqlite_to_pg.py <sqlite-file> [--schema NAME] [--dry-run]
DSN: $MNEMO_PG_DSN, else $HERMES_HOME/.env, else private lcm-pg cluster.
"""
from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from mnemo_backends import psycopg_site as _ps
_ps.ensure_psycopg()

import argparse
import os
import pathlib
import sqlite3
import sys

import psycopg


def get_dsn() -> str:
    d = os.environ.get("MNEMO_PG_DSN")
    if d:
        return d
    home = pathlib.Path(os.environ.get("HERMES_HOME") or pathlib.Path.home() / ".hermes")
    for envf in (home / ".env", pathlib.Path.home() / ".hermes" / ".env"):
        if envf.is_file():
            for line in envf.read_text().splitlines():
                if line.startswith("MNEMO_PG_DSN="):
                    return line.split("=", 1)[1].strip()
    run = pathlib.Path.home() / "lcm-pg" / "run"
    if run.is_dir():
        return f"postgresql://lcm01@/lcm?host={run}&port=54329"
    sys.exit("no DSN: set MNEMO_PG_DSN")


SKIP_TABLES = {"sqlite_sequence"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sqlite_file")
    ap.add_argument("--schema", default=None,
                    help="target schema (default: $MNEMO_PG_SCHEMA or "
                         "mnemosyne_<profile from HERMES_HOME>)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    schema = a.schema or os.environ.get("MNEMO_PG_SCHEMA")
    if not schema:
        home = os.environ.get("HERMES_HOME", "")
        prof = "default" if not home or home.rstrip("/").endswith(".hermes") \
            else home.rstrip("/").rsplit("/", 1)[-1]
        schema = f"mnemosyne_{prof}"

    src = sqlite3.connect(f"file:{a.sqlite_file}?mode=ro", uri=True)
    tables = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name") if r[0] not in SKIP_TABLES]

    with psycopg.connect(get_dsn(), autocommit=False) as pg:
        pg.execute(f'SET search_path TO "{schema}", public')
        exists = {r[0] for r in pg.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s", (schema,))}
        total = 0
        for t in tables:
            if t not in exists:
                print(f"  {t}: SKIP (not in PG schema)")
                continue
            cols = [r[1] for r in src.execute(f"PRAGMA table_info('{t}')")]
            n_src = src.execute(f"SELECT COUNT(*) FROM '{t}'").fetchone()[0]
            n_dst = pg.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            if n_src == 0:
                print(f"  {t}: src empty")
                continue
            if n_dst >= n_src:
                print(f"  {t}: PG has {n_dst} >= sqlite {n_src} -> SKIP")
                continue
            missing = pg.execute(
                f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            collist = ", ".join(f'"{c}"' for c in cols)
            placeholders = ", ".join(["%s"] * len(cols))
            rows = src.execute(f"SELECT {', '.join(cols)} FROM '{t}'").fetchall()
            if a.dry_run:
                print(f"  {t}: DRY {n_src} rows -> {n_dst}+{n_src - n_dst}")
                continue
            # insert rows whose primary key (first col assumed id/memory_id style)
            # is absent in PG; simplest deterministic approach: copy ALL and
            # let conflict on any unique index surface; use ON CONFLICT DO
            # NOTHING keyed on the first column if it is unique in PG.
            uniq_first = pg.execute("""
                SELECT 1 FROM pg_index i JOIN pg_attribute a
                  ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
                JOIN pg_class c ON c.oid=i.indrelid
                JOIN pg_namespace ns ON ns.oid=c.relnamespace
                WHERE ns.nspname=%s AND c.relname=%s AND a.attname=%s
                  AND i.indisunique AND i.indnatts=1""",
                (schema, t, cols[0])).fetchone()
            stmt = f'INSERT INTO "{t}" ({collist}) VALUES ({placeholders})'
            if uniq_first:
                stmt += f' ON CONFLICT ("{cols[0]}") DO NOTHING'
            with pg.cursor() as cur, cur.copy(stmt) as cp:
                for row in rows:
                    cp.write_row(row)
            n_after = pg.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            total += n_after - n_dst
            print(f"  {t}: {n_src} sqlite / {n_dst} pg -> {n_after} pg")
        # sequences
        for t in tables:
            seqs = pg.execute("""
                SELECT pg_get_serial_sequence(quote_ident(ns.nspname)||'.'||quote_ident(c.relname), a.attname), a.attname
                FROM pg_class c JOIN pg_namespace ns ON ns.oid=c.relnamespace
                JOIN pg_attribute a ON a.attrelid=c.oid
                WHERE ns.nspname=%s AND c.relname=%s AND a.attnum>0
                  AND pg_get_serial_sequence(quote_ident(ns.nspname)||'.'||quote_ident(c.relname), a.attname) IS NOT NULL""",
                (schema, t)).fetchall()
            for s, col in seqs:
                try:
                    pg.execute(f"SELECT setval(%s, COALESCE((SELECT MAX(\"{col}\") FROM \"{schema}\".\"{t}\"), 1) + 1, false)", (s,))
                except Exception as exc:
                    print(f"  seq {s}: {exc}")
        if a.dry_run:
            pg.rollback()
            print("dry-run: rolled back")
        else:
            pg.commit()
            print(f"committed; {total} rows copied into {schema}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
