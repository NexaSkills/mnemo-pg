"""Rebuild vec_<t> companion tables from PG-native memory_embeddings.

migrate_sqlite.py's rebuild_vectors reads from a SQLite source; this is the
in-PG equivalent (idempotent, ON CONFLICT upsert) used to heal vec tables
that lag their embeddings (e.g. mnemosyne_mike vec_episodes=0 with populated
episodic_memory, or vec_working drift).

Rowid mapping: base tables expose rowid via the shim column
<table>__rowid (working_memory/facts/...) or a real identity (episodic
memory schemas that have one); resolved per table.

Usage: python3 scripts/rebuild_vec_from_pg.py <schema>
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import os
import sys

from mnemo_backends import psycopg_site as _ps
_p = _ps.ensure_psycopg()
import psycopg


def dsn():
    d = os.environ.get("MNEMO_PG_DSN", "").strip()
    if d:
        return d
    return ("host=%s port=54329 dbname=lcm user=lcm01"
            % os.path.expanduser("~/lcm-pg/run"))


def main():
    schema = sys.argv[1]
    if not schema.replace("_", "isalnum").isalnum():
        raise SystemExit("unsafe schema name")
    dim = int(os.environ.get("MNEMOSYNE_EMBEDDING_DIM", "384"))
    conn = psycopg.connect(dsn(), autocommit=True)
    cur = conn.cursor()
    cur.execute('SET search_path = "%s", public' % schema)
    for base, vec_t, key in (("working_memory", "vec_working", "id"),
                            ("episodic_memory", "vec_episodes", "id"),
                            ("facts", "vec_facts", "fact_id")):
        cur.execute(
            "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() AND c.relname=%s", (vec_t,))
        if cur.fetchone() is None:
            print(f"{vec_t}: absent, skip")
            continue
        # rowid source: real column 'rowid', else the shim column
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE "
            "table_schema=current_schema() AND table_name=%s "
            "AND column_name='rowid'", (base,))
        if cur.fetchone():
            rid_col = "rowid"
        else:
            rid_col = '"%s__rowid"' % base
        n = 0
        skipped = 0
        cur.execute("SELECT memory_id, embedding_json FROM memory_embeddings")
        rows = cur.fetchall()
        for eid, ej in rows:
            import json
            try:
                vec = json.loads(ej)
            except Exception:
                skipped += 1
                continue
            if not isinstance(vec, list) or len(vec) != dim:
                skipped += 1
                continue
            norm = sum(x * x for x in vec) ** 0.5
            if norm == 0:
                skipped += 1
                continue
            unit = [x / norm for x in vec]
            r = cur.execute(
                'SELECT %s FROM "%s" WHERE "%s" = %%s' % (rid_col, base, key),
                (eid,)).fetchone()
            if r is None:
                skipped += 1
                continue
            rid = r[0]
            cur.execute(
                'INSERT INTO "%s" (rowid, embedding) VALUES (%%s, %%s::vector) '
                "ON CONFLICT (rowid) DO UPDATE SET embedding = EXCLUDED.embedding"
                % vec_t,
                (rid, "[" + ",".join("%.7f" % x for x in unit) + "]"))
            n += 1
        cur.execute('SELECT count(*) FROM "%s"' % vec_t)
        total = cur.fetchone()[0]
        print(f"{vec_t}: placed {n} (skipped {skipped}), table now {total}")
    conn.close()


if __name__ == "__main__":
    main()
