# fp32+embedding shape: line trace (no monkeypatch) to find head residue.
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import native_conn as nc

q = "SELECT rowid, distance, embedding FROM vec_episodes WHERE embedding MATCH ? AND k=5 ORDER BY distance"

print("drop_companion output:", nc._drop_companion_blob_projections(q))

def tracer(frame, event, arg):
    if frame.f_code.co_name not in ("_rewrite_vec_knn_impl", "_drop_companion_blob_projections"):
        return None
    if event == "return":
        print(f"  {frame.f_code.co_name} RET@{frame.f_lineno} -> ...{str(arg)[-40:]!r}")
    return tracer

sys.settrace(tracer)
try:
    out = nc._rewrite_vec_knn(q)
    print("FINAL:", out)
finally:
    sys.settrace(None)
