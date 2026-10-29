#!/usr/bin/env python3
"""Check vector coverage per schema: every base row must have a vec_* embedding.

Usage: check_vec_coverage.py [--all | <schema> ...]
Exit 0 if every schema is fully covered, 1 otherwise.
"""
import os, sys, psycopg

PAIRS = (
    ("working_memory", "vec_working", "id", "working_memory__rowid"),
    ("episodic_memory", "vec_episodes", "id", "episodic_memory__rowid"),
    ("facts", "vec_facts", "fact_id", "facts__rowid"),
)

def dsn():
    d = os.environ.get("MNEMO_PG_DSN")
    if d:
        return d
    return dict(host=os.path.expanduser(os.environ.get("MNEMO_PG_HOST", "~/lcm-pg/run")),
                port=int(os.environ.get("MNEMO_PG_PORT", "54329")),
                dbname=os.environ.get("MNEMO_PG_DB", "lcm"),
                user=os.environ.get("MNEMO_PG_USER", "lcm01"))

def check(conn, schema):
    if not schema.replace("_", "isalnum").isalnum():
        raise SystemExit("unsafe schema name")
    cur = conn.cursor()
    cur.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema,))
    if cur.fetchone() is None:
        print(f"{schema}: absent, skip")
        return True
    cur.execute('SET search_path = "%s", public' % schema)
    ok = True
    for base, vec_t, key, rcol in PAIRS:
        cur.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=current_schema() AND c.relname=%s", (vec_t,))
        if cur.fetchone() is None:
            print(f"  {base}: {vec_t} absent, skip")
            continue
        miss = cur.execute(
            f"SELECT count(*) FROM {base} b WHERE NOT EXISTS "
            f"(SELECT 1 FROM {vec_t} v WHERE v.rowid = b.{rcol})").fetchone()[0]
        total = cur.execute(f"SELECT count(*) FROM {base}").fetchone()[0]
        print(f"  {base}: {total - miss}/{total} covered" + ("" if miss == 0 else f"  MISSING {miss}"))
        ok = ok and miss == 0
    return ok

def main():
    args = sys.argv[1:]
    d = dsn()
    conn = psycopg.connect(**d) if isinstance(d, dict) else psycopg.connect(d)
    conn.autocommit = True
    if args == ["--all"] or not args:
        schemas = [r[0] for r in conn.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'mnemosyne%' ORDER BY 1").fetchall()]
    else:
        schemas = args
    ok = all(check(conn, s) for s in schemas)
    print("COVERAGE:", "OK" if ok else "GAPS")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
