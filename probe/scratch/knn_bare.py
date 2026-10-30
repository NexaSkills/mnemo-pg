# Trace driver frames for the bare shape (expect t 5).
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()
assert nc._companion_is_vector("vec_episodes")

q = "SELECT rowid, distance FROM vec_episodes WHERE embedding MATCH ? AND k=5 ORDER BY distance"

def tracer(frame, event, arg):
    if frame.f_code.co_name not in ("_rewrite_vec_knn_impl", "_rewrite_vec_knn_body"):
        return None
    if event == "call":
        print(f"CALL {frame.f_code.co_name} sql=...{frame.f_locals['sql'][-44:]!r}")
    if event == "return":
        print(f"RET  {frame.f_code.co_name}@{frame.f_lineno} ...{str(arg)[-44:]!r}")
    return tracer

sys.settrace(tracer)
try:
    out = nc._rewrite_vec_knn(q)
    print("FINAL:", out)
finally:
    sys.settrace(None)
