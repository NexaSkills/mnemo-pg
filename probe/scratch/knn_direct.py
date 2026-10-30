# Direct call, real module binding: does the short-splice branch fire?
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()

def tracer(frame, event, arg):
    if frame.f_code.co_name != "_rewrite_vec_knn_impl": return None
    if event == "line":
        f = frame.f_locals
        name = frame.f_code.co_filename
        if frame.f_lineno in (1212, 1215, 1216, 1222, 1227, 1229) :
            print(f"  L{frame.f_lineno} short={str(f.get('_short'))[:60]!r} out={'None' if f.get('_out') is None else str(f.get('_out'))[-20:]+'' !r}")
    if event == "return":
        print(f"  RET@{frame.f_lineno} -> {str(arg)[-40:]!r}")
    return tracer

q = "SELECT rowid, distance FROM vec_episodes WHERE embedding MATCH ? AND k=5 ORDER BY distance"
print("module file:", nc.__file__)
print("impl qual:", nc._rewrite_vec_knn_impl.__module__)
sys.settrace(tracer)
try:
    out = nc._rewrite_vec_knn(q)
    print("FINAL:", out)
finally:
    sys.settrace(None)
c.close()
