# Direct unit-trace of _rewrite_vec_knn_impl on the 5 shapes (no binds).
import sys, re
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')

from mnemo_backends import native_conn as nc

# instrument: wrap _rewrite_vec_knn_impl recursion visibility
impl = nc._rewrite_vec_knn_impl

VEC = "[0.1,0.2,0.3]"
qs = {
 "int8-literal": f"SELECT rowid, distance FROM vec_working WHERE embedding MATCH vec_quantize_int8('{VEC}', \"unit\") AND k=5 ORDER BY distance",
 "fp32-literal": f"SELECT rowid, distance FROM vec_working WHERE embedding MATCH '{VEC}' AND k=5 ORDER BY distance",
}
for name, q in qs.items():
    print("="*70)
    print("IN :", q)
    m = nc._VEC_KNN_RE.search(q)
    print("VEC_KNN_RE match:", repr(m.group(0)) if m else None, "groups:", m.groupdict() if m else None)
    kw = nc._VEC_KNN_AND_K_RE.search(q, m.start()) if m else None
    print("AND_K match end==span end:", kw is not None and kw.end()==m.end(), repr(kw.group(0)) if kw else None)
    out = impl(q)
    print("OUT:", out if out is q else repr(out))
