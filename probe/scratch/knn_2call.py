# Second call to _rewrite_vec_knn_impl: what SQL shape triggers the generic epilogue?
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()

_orig = nc._rewrite_vec_knn_impl
def traced(sql):
    print("IMPL-IN :", sql[:200])
    out = _orig(sql)
    print("IMPL-OUT:", ("<IDENT>" if out is sql else out[:200]))
    return out
nc._rewrite_vec_knn_impl = traced

q = "SELECT rowid, distance FROM vec_working WHERE embedding MATCH ? AND k=5 ORDER BY distance"
try:
    rows = c.execute(q, ("[0.1,0.2,0.3]",)).fetchall()
    print("rows:", len(rows))
except Exception as e:
    print("err:", type(e).__name__, str(e)[:120])
c.close()
