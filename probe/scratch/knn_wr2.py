# fp32+emb shape, direct impl call, line trace of the wrapper branch.
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import native_conn as nc

q = "SELECT rowid, distance, embedding FROM vec_episodes WHERE embedding MATCH ? AND k=5 ORDER BY distance"

def tracer(frame, event, arg):
    if frame.f_code.co_name != "_rewrite_vec_knn_impl":
        return None
    if event == "line" and 1360 <= frame.f_lineno <= 1440:
        vars_txt = ""
        try:
            vars_txt = " head2=" + repr(frame.f_locals.get("head2"))[-46:]
        except Exception:
            pass
        print(f"L{frame.f_lineno}{vars_txt}")
    if event == "return":
        print(f"RET@{frame.f_lineno} ...{str(arg)[-50:]!r}")
    return tracer

sys.settrace(tracer)
try:
    out = nc._rewrite_vec_knn_impl(q)
    print("FINAL:", out)
finally:
    sys.settrace(None)
