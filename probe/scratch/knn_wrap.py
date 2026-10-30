# Reproduce the exact SQL that Cursor.execute emits for the bare-? KNN shape.
import sys, re
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')

# hook _rewrite_vec_knn to see input/output at the call site
orig_impl = nc._rewrite_vec_knn
def traced(sql):
    out = orig_impl(sql)
    if "MATCH" in sql:
        print("[WR] IN :", sql)
        print("[WR] OUT:", "<IDENTITY>" if out is sql else out)
    return out
nc._rewrite_vec_knn = traced

q = "SELECT rowid, distance FROM vec_working WHERE embedding MATCH ? AND k=5 ORDER BY distance"
try:
    rows = c.execute(q, ("[0.1,0.2,0.3]",)).fetchall()
    print("rows:", len(rows))
except Exception as e:
    print("err:", type(e).__name__, str(e)[:200])
c.close()
