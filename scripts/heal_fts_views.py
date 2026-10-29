#!/usr/bin/env python3
"""One-shot heal: rebuild FTS shadow facade views in every mnemo-pg schema.

Root cause of stale views: `pg_index.indkey` is int2vector; `= ANY(array)`
compares int2 against int4 and finds no rows, so the PK lookup silently
returned NULL and the view fell back to `t.oid` -- which under a schema's
search_path resolves to the schema's own `date(text)` parity shim, giving
"column <table>.oid does not exist". Fix: cast indkey to int4[] explicitly.
"""
import os
import sys
import psycopg

DSN = dict(host=os.path.expanduser("~/lcm-pg/run"), port=54329,
           user="lcm01", dbname="lcm", autocommit=True)
SCHEMAS = sys.argv[1:] or [
    "mnemosyne_mike", "mnemosyne_default", "mnemosyne_brandbuddy",
    "mnemosyne_debbie", "mnemosyne_buttercup", "mnemosyne_shared",
]
SPECS = (  # (view, content table, indexed col, tsv col)
    ("fts_working", "working_memory", "content", "content_tsv"),
    ("fts_episodes", "episodic_memory", "content", "content_tsv"),
    ("fts_facts", "facts", "object", "object_tsv"),
)


def heal(cur, schema):
    cur.execute('SET search_path TO "%s", public' % schema)

    def col_exists(table, col):
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=%s "
            "AND column_name=%s LIMIT 1", (table, col))
        return cur.fetchone() is not None

    def pk_of(table):
        # NOTE: explicit ::int4[] cast -- int2vector = ANY(int4[]) silently
        # matches nothing (int2 vs int4 type mismatch).
        cur.execute(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "JOIN pg_attribute a ON a.attnum = ANY(i.indkey::int4[]) "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = current_schema() AND t.relname = %s "
            "AND i.indisprimary LIMIT 1", (table,))
        r = cur.fetchone()
        return r[0] if r else None

    for view, content, col, tsv in SPECS:
        rowid_src = ('"%s"."rowid"' % content) if col_exists(content, "rowid") \
            else ('"%s"."%s__rowid"' % (content, content)
                  if col_exists(content, content + "__rowid") else "NULL")
        pk = pk_of(content)
        id_col = ('"%s"."%s" AS "id"' % (content, pk)) if pk \
            else "%s.rowid AS \"id\"" % content if rowid_src != "NULL" else "NULL"
        rank = ("COALESCE(-ts_rank(\"%s\", to_tsquery('english','x')), 0)::float8"
                % tsv) if col_exists(content, tsv) else "0::float8"
        sql = ('CREATE OR REPLACE VIEW "%s" AS SELECT %s AS "rowid", %s AS '
               '"rank", "%s" AS "%s", %s FROM "%s"'
               % (view, rowid_src, rank, col, col, id_col, content))
        try:
            cur.execute(sql)
            print(schema, view, "OK (pk=%s)" % pk)
        except Exception as e:
            print(schema, view, "ERR", str(e).splitlines()[0][:120])


def main():
    raw = psycopg.connect(**DSN)
    cur = raw.cursor()
    for s in SCHEMAS:
        try:
            heal(cur, s)
        except Exception as e:
            print(s, "SKIP", str(e).splitlines()[0][:80])
    raw.close()


if __name__ == "__main__":
    main()
