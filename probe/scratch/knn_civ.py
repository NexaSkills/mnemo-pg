# Companion check per schema + call-site trace of _companion_is_vector.
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
from mnemo_backends import native_conn as nc

# call it directly with no conn (probe path)
print("direct probe call:", nc._companion_is_vector('vec_working'), "schema:", nc._CURRENT_SCHEMA[0] or nc._active_schema())

# trace every call from the KNN rewrite
orig = nc._companion_is_vector
def traced(table):
    r = orig(table)
    import traceback
    print(f"[CIV] table={table} schema={nc._CURRENT_SCHEMA[0] or nc._active_schema()} conn={'set' if nc._ACTIVE_CONN[0] else 'None'} -> {r}")
    return r
nc._companion_is_vector = traced

import plugin
from mnemo_backends import factory
c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
try:
    r = c.recall("vector test query", limit=3) if hasattr(c,'recall') else None
except Exception as e:
    print("recall err:", type(e).__name__, str(e)[:120])
# direct KNN query
q = "SELECT rowid, distance FROM vec_working WHERE embedding MATCH ? AND k=5 ORDER BY distance"
try:
    rows = c.execute(q, ("[0.1,0.2,0.3]",)).fetchall()
    print("knn rows:", len(rows))
except Exception as e:
    print("knn err:", type(e).__name__, str(e)[:200])
c.close()
