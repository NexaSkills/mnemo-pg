# Stage trace through the REAL Cursor.execute for the fp32+emb shape.
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import native_conn as nc
import mnemo_backends.fts_rewrite as fr
import mnemo_backends.params as _p

_q = "SELECT rowid, distance, embedding FROM vec_episodes WHERE embedding MATCH ? AND k=5 ORDER BY distance"

_traced = []

# watch every string-returning function that touches our query
import mnemo_backends.binding as _b

def wrap(mod, fname):
    orig = getattr(mod, fname, None)
    if orig is None or not callable(orig):
        return
    def w(*a, **k):
        r = orig(*a, **k)
        if isinstance(a[0] if a else None, str) and "MATCH" in str(a[0]) and isinstance(r, (str, tuple)):
            rr = r[0] if isinstance(r, tuple) else r
            if isinstance(rr, str) and rr != a[0]:
                _traced.append((mod.__name__ + "." + fname,
                                "IN ..." + a[0][-60:],
                                "OUT ..." + rr[-60:]))
        return r
    setattr(mod, fname, w)

for f in ("rewrite_fts_sql", "rewrite_rowids"):
    wrap(fr, f)
    import importlib
for name in dir(_p):
    o = getattr(_p, name)
    if callable(o) and getattr(o, "__module__", "") == _p.__name__:
        wrap(_p, name)

import sqlite3 as _s3
conn = _s3.connect("mnemosyne.db", uri=True,
                   module=nc, schema="mnemosyne_mike")
cur = conn.cursor()
try:
    cur.execute(_q, [bytes(384*4)])
    print("OK rows:", len(cur.fetchall()))
except Exception as e:
    print("ERR", type(e).__name__, str(e)[:80])
for t in _traced:
    print("TRACE:", t[0])
    print("  ", t[1])
    print("  ", t[2])
