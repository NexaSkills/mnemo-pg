# Full pipeline diff trace on the EXACT shapes the stores issue.
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc, rowid_rewrite, fts_rewrite

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()

shape = "SELECT rowid, embedding, distance FROM vec_working WHERE embedding MATCH ? AND k = ? ORDER BY distance LIMIT ?"
params = ("[0.1,0.2,0.3]", 5, 5)

def pipeline(q):
    s = q
    s2, ch = _rowid_hook(s); print("rowid:", s2[:160]) if "vec_working" in s2 else None
    s3, ch2 = _fts_hook(s2); print("fts  :", s3[:160]) if "vec_working" in s3 else None
    k = nc._rewrite_vec_knn(s3)
    print("knn  :", "<IDENT>" if k is s3 else k[:200])
    return k

_r = rowid_rewrite.rewrite_rowids
_f = fts_rewrite.rewrite_fts_sql
def _rowid_hook(s): return _r(s, "mnemosyne_mike", None) if False else (s, False)
def _fts_hook(s): return _f(s)

def tracer(frame, event, arg):
    if frame.f_code.co_name != "_rewrite_vec_knn_impl":
        return None
    if event == "return":
        print(f"  RET@{frame.f_lineno} tail={str(frame.f_locals.get('_tail'))[:40]!r} out={str(arg)[-25:]!r}")
    return tracer

sys.settrace(tracer)
try:
    rows = c.execute(shape, params).fetchall()
    print("rows:", len(rows))
except Exception as e:
    print("err:", type(e).__name__, str(e)[:180])
finally:
    sys.settrace(None); c.close()
