#!/usr/bin/env python3
"""Engine-level recall probe on the PG path: create Mnemosyne, ZEBRA-remember,
recall, trace executed SQL."""
import os, sys, re, pathlib
os.environ["MNEMO_BACKEND"] = "postgres"
os.environ["HERMES_HOME"] = str(pathlib.Path.home() / ".hermes/profiles/mike")
sys.path.insert(0, str(pathlib.Path.home() / "workspace/mnemo-pg/repo"))
sys.path.insert(0, str(pathlib.Path.home() / "workspace/mnemo-pg/repo/vendor"))

from mnemo_backends import install
from mnemo_backends.install import install as _bi; _bi()
import mnemosyne_hermes as boot

SEEN = []
import mnemo_backends.postgres as pgmod
orig = pgmod._Cursor.execute
def traced(self, sql, params=None):
    SEEN.append(re.sub(r"\s+", " ", str(sql))[:200])
    return orig(self, sql, params)
for _mn in ("postgres", "fts_conn", "fts_pg"):
    try:
        _m = __import__("mnemo_backends." + _mn, fromlist=["x"])
        for _nm in dir(_m):
            _c = getattr(_m, _nm)
            if isinstance(_c, type) and "execute" in _c.__dict__:
                _o = _c.__dict__["execute"]
                if getattr(_o, "_mnemo_traced", False):
                    continue
                def _mk(o):
                    def t(self, sql, params=None, _o=o):
                        SEEN.append(re.sub(r"\s+", " ", str(sql))[:200])
                        return _o(self, sql, params)
                    t._mnemo_traced = True
                    return t
                _c.execute = _mk(_o)
                print("traced:", _mn, _nm)
    except Exception as _e:
        print(_mn, "skip", _e)

from mnemosyne.core.memory import Mnemosyne
p = pathlib.Path.home() / ".hermes/profiles/mike/.mnemosyne/mnemosyne.db"
eng = Mnemosyne(db_path=str(p))
print("engine created OK (PG-backed)")

mid = eng.remember("ZEBRA99 the striped horse stands on the roof", scope="global", importance=0.9)
print("remember ->", mid)
SEEN.clear()
_c0 = _real_connect = None
import mnemo_backends.factory as _f
try:
    _c0 = _f.connect(str(p))
    n = _c0.execute("SELECT count(*) FROM working_memory").fetchone()[0]
    print("direct probe: working_memory rows:", n)
    hits = _c0.execute("SELECT id FROM working_memory WHERE content LIKE ? LIMIT 3", ("%ZEBRA99%",)).fetchall()
    print("direct LIKE hits:", [h[0][:12] for h in hits])
except Exception as e:
    import traceback; traceback.print_exc()
try:
    import mnemosyne.core.beam as _beam
    _orig_vec = _beam._wm_vec_search
    def _tv(conn, emb, k=20, **kw):
        import traceback
        try:
            r = _orig_vec(conn, emb, k=k, **kw)
            print("  _wm_vec_search OK:", r[:3])
            return r
        except Exception as e:
            print("  _wm_vec_search RAISED:", type(e).__name__, str(e)[:140])
            raise
    _beam._wm_vec_search = _tv
    res = eng.recall("ZEBRA99", top_k=5)
    items = res.get("results", res) if isinstance(res, dict) else res
    out = []
    for r in list(items)[:5]:
        if isinstance(r, dict):
            out.append((r.get("id"), r.get("layer"), round(float(r.get("score") or 0), 3)))
        else:
            out.append(r)
    print("recall:", out)
except Exception:
    import traceback; traceback.print_exc()
vec = [s for s in SEEN if "vec_" in s or "<=>" in s]
print(f"statements: {len(SEEN)}  vector-related: {len(vec)}")
import collections
buckets = collections.Counter()
for s in SEEN:
    u = s.upper()
    if "EMBEDDING" in u or "VEC_" in u or "VECTOR" in u or "SIMILAR" in u or "MATCH" in u:
        buckets["vector-ish"] += 1
        print("  V?", s[:165])
    elif "TSQUERY" in u or "TSVECTOR" in u or "TS_RANK" in u:
        buckets["fts"] += 1
        print("  F:", s[:165])
print("buckets:", dict(buckets))
for s in SEEN[:6]:
    print("  S:", s[:130])
