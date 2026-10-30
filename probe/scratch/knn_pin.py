# Pinpoint the line that appends ' 5' after the clean wrapper return.
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc, rowid_rewrite, fts_rewrite

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()

# patch the pipeline hooks in Cursor.execute to watch sql_s after each
for name in ("rewrite_rowids",):
    orig = getattr(rowid_rewrite, name)
    def mk(orig=orig, name=name):
        def f(*a, **k):
            r = orig(*a, **k)
            s = r[0] if isinstance(r, tuple) else r
            if isinstance(s, str) and "vec_working" in s:
                print(f"AFTER {name}:", repr(s[-70:]))
            return r
        return f
    setattr(rowid_rewrite, name, mk())

orig_rw = fts_rewrite.rewrite_fts_sql
def rw(*a, **k):
    r = orig_rw(*a, **k)
    s = r[0] if isinstance(r, tuple) else r
    if isinstance(s, str) and "vec_working" in s:
        print("AFTER fts_rewrite:", repr(s[-70:]))
    return r
fts_rewrite.rewrite_fts_sql = rw

q = "SELECT rowid, distance FROM vec_working WHERE embedding MATCH ? AND k=5 ORDER BY distance"
try:
    rows = c.execute(q, ("[0.1,0.2,0.3]",)).fetchall()
    print("rows:", len(rows))
except Exception as e:
    print("err:", type(e).__name__, str(e)[:150])
c.close()
