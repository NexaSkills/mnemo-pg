# Line-trace the recursion frames for the fp32-only shape (no monkeypatch).
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import native_conn as nc

q = "SELECT rowid, distance FROM vec_episodes WHERE embedding MATCH ? AND k=5 ORDER BY distance"

frames = []
def tracer(frame, event, arg):
    if frame.f_code.co_name != "_rewrite_vec_knn_impl":
        return None
    if event == "call":
        frames.append(frame)
    if event == "line":
        print(f"  d{len(frames)} L{frame.f_lineno}")
    if event == "return":
        print(f"  d{len(frames)} RET@{frame.f_lineno} -> ...{str(arg)[-28:]!r}")
        frames.pop()
    return tracer

sys.settrace(tracer)
try:
    out = nc._rewrite_vec_knn(q)
    print("FINAL:", out)
finally:
    sys.settrace(None)
