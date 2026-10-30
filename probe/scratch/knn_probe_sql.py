# Compare vec0 probe SQL between isolated probe and real gateway env.
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

print("DSN:", factory._pg_dsn()[:60])
print("companion vec_episodes:", nc._companion_is_vector("vec_episodes"))
print("companion vec_working:", nc._companion_is_vector("vec_working"))

# replicate the probe
from mnemo_backends import native_conn as N
probe = None
try:
    probe = N._SQLITE_MASTER_VEC_SQL.replace("@TABLE@", "vec_episodes")
except Exception as e:
    print("no _SQLITE_MASTER_VEC_SQL:", e)
print("PROBE SQL:", probe)

import psycopg
dsn = factory._pg_dsn()
with psycopg.connect(dsn) as c:
    row = c.execute(probe).fetchone() if probe else None
    print("PROBE RESULT:", row)
    # what does vec_episodes actually look like?
    rows = c.execute("""
      SELECT a.attname, format_type(a.atttypid,a.atttypmod)
      FROM pg_attribute a JOIN pg_class cl ON cl.oid=a.attrelid
      JOIN pg_namespace ns ON ns.oid=cl.relnamespace
      WHERE cl.relname='vec_episodes' AND a.attnum>0
      ORDER BY a.attnum""").fetchall()
    print("vec_episodes cols:", rows)
