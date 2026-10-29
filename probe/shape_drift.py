#!/usr/bin/env python3
"""Compare native_ddl TABLES columns vs live schema columns."""
import re, sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo/mnemo_backends')
import psycopg_site; psycopg_site.ensure_psycopg()
import psycopg

DDL = '/home/mybuddy/workspace/mnemo-pg/repo/scripts/native_ddl.py'
src = open(DDL).read()
tables = re.findall(r'\("(\w+)", \[\n(.*?)\n\s*\], \[\]\)', src, re.S)

schema = sys.argv[1] if len(sys.argv) > 1 else 'mnemosyne_mike'
conn = psycopg.connect('host=/home/mybuddy/lcm-pg/run port=54329 dbname=lcm user=lcm01')
cur = conn.cursor()
cur.execute("""SELECT c.relname, string_agg(a.attname, ' ' ORDER BY a.attnum)
FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname=%s AND c.relkind='r' AND a.attnum>0 GROUP BY 1""", (schema,))
live = {r[0]: set(r[1].split()) for r in cur.fetchall()}

print(f"DDL tables in script: {len(tables)}")
for name, cols in tables:
    ddl = set(re.findall(r'"(\w+)[ "]', cols))
    if name not in live:
        print(f"{name:28} NOT-IN-LIVE")
        continue
    miss = ddl - live[name] - {name + '__rowid'}
    extra = live[name] - ddl - {name + '__rowid'}
    if miss or extra:
        print(f"{name:28} miss={sorted(miss)} extra={sorted(extra)}")
print('done')
