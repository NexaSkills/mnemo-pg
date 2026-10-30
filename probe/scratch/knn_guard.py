# Trace every line 1430-1470 with body value, bare shape.
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()

q = "SELECT rowid, distance FROM vec_episodes WHERE embedding MATCH ? AND k=5 ORDER BY distance"

def tracer(frame, event, arg):
    if frame.f_code.co_name != "_rewrite_vec_knn_body":
        return None
    if event == "line" and 1436 <= frame.f_lineno <= 1452:
        b = frame.f_locals.get("body")
        print(f"L{frame.f_lineno}" + (f" body={b[-26:]!r} head2={str(frame.f_locals.get("head2"))[-30:]!r} tail2={str(frame.f_locals.get("tail2"))[-26:]!r} outer_tail={str(frame.f_locals.get("outer_tail"))[-20:]!r}" if isinstance(b,str) else ""))
    if event == "return":
        print(f"RET@{frame.f_lineno}")
    return tracer

sys.settrace(tracer)
try:
    out = nc._rewrite_vec_knn(q)
    print("FINAL:...", out[-26:])
finally:
    sys.settrace(None)
