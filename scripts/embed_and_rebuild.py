#!/usr/bin/env python3
"""Embed base rows lacking embeddings, then rebuild vec_* tables.

Usage: embed_and_rebuild.py <schema> [--limit N] [--table episodic_memory|facts|all]
"""
import os, sys, json, psycopg

def dsn():
    d = os.environ.get("MNEMO_PG_DSN")
    if d:
        return d
    return dict(host=os.path.expanduser(os.environ.get("MNEMO_PG_HOST", "~/lcm-pg/run")),
                port=int(os.environ.get("MNEMO_PG_PORT", "54329")),
                dbname=os.environ.get("MNEMO_PG_DB", "lcm"),
                user=os.environ.get("MNEMO_PG_USER", "lcm01"))

TABLES = {
    "episodic_memory": ("vec_episodes", "id", "content", "rowid"),
    "facts": ("vec_facts", "fact_id",
              "subject || ' ' || predicate || ' ' || object", "facts__rowid"),
}

def main():
    schema = sys.argv[1]
    if not schema.replace("_", "isalnum").isalnum():
        raise SystemExit("unsafe schema name")
    want = "all"
    limit = 0
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--limit":
            limit = int(args[i + 1]); i += 2
        elif args[i] == "--table":
            want = args[i + 1]; i += 2
        else:
            i += 1

    from mnemosyne.core.embeddings import embed_query

    d = dsn()
    conn = psycopg.connect(**d) if isinstance(d, dict) else psycopg.connect(d)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute('SET search_path = "%s", public' % schema)
    tables = list(TABLES) if want == "all" else [want]
    for base in tables:
        vec_t, key, text_expr, rcol = TABLES[base]
        q = (f"SELECT {key}, {text_expr} AS txt FROM {base} b "
             "WHERE NOT EXISTS (SELECT 1 FROM memory_embeddings m WHERE m.memory_id = b.%s)" % key)
        if limit:
            q += " LIMIT %d" % limit
        rows = cur.execute(q).fetchall()
        placed = 0
        skipped = 0
        for mid, txt in rows:
            try:
                v = embed_query((txt or "")[:2000])
                vec = "[" + ",".join("%.8f" % float(x) for x in v) + "]"
                cur.execute(
                    "INSERT INTO memory_embeddings (memory_id, embedding_json) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING", (mid, vec))
                placed += 1
            except Exception:
                skipped += 1
        n = cur.execute(
            f"INSERT INTO {vec_t} (rowid, embedding) "
            f"SELECT b.{rcol}, m.embedding_json::vector FROM {base} b "
            f"JOIN memory_embeddings m ON m.memory_id = b.{key} "
            f"ON CONFLICT (rowid) DO UPDATE SET embedding = EXCLUDED.embedding").rowcount
        now = cur.execute(f"SELECT count(*) FROM {vec_t}").fetchone()[0]
        print(f"{base}: embedded {placed}/{len(rows)} (errors {skipped}), "
              f"{vec_t} now {now}", flush=True)

if __name__ == "__main__":
    main()
