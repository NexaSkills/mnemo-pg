# Trace head2/outer_sel/body inside the wrapper branch with companion True.
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()
assert nc._companion_is_vector("vec_episodes")

q = "SELECT rowid, distance, embedding FROM vec_episodes WHERE embedding MATCH ? AND k=5 ORDER BY distance"

prev = {}
def tracer(frame, event, arg):
    if frame.f_code.co_name != "_rewrite_vec_knn_impl":
        return None
    if event == "line" and 1360 <= frame.f_lineno <= 1436:
        loc = frame.f_locals
        snap = {}
        for kname in ("head2", "outer_sel", "outer_tail", "body"):
            v = loc.get(kname)
            if v is not None and prev.get(kname) != v:
                print(f"L{frame.f_lineno} {kname} = ...{str(v)[-56:]!r}")
                prev[kname] = v
    if event == "return":
        print(f"RET@{frame.f_lineno} ...{str(arg)[-56:]!r}")
    return tracer

sys.settrace(tracer)
try:
    out = nc._rewrite_vec_knn(q)
    print("FINAL:", out)
finally:
    sys.settrace(None)
