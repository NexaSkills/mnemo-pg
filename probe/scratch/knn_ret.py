# Return-site trace inside _rewrite_vec_knn_impl (depth-aware).
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()

def tracer(frame, event, arg):
    if frame.f_code.co_name != "_rewrite_vec_knn_impl":
        return None
    if event == "return":
        print(f"RETURN@{frame.f_lineno} sql={frame.f_locals.get('sql','?')[:80]!r} body={str(arg)[-30:]!r}")
    return tracer

q = "SELECT rowid, distance FROM vec_working WHERE embedding MATCH ? AND k=5 ORDER BY distance"
sys.settrace(tracer)
try:
    c.execute(q, ("[0.1,0.2,0.3]",)).fetchall()
except Exception as e:
    print("err:", type(e).__name__, str(e)[:80])
finally:
    sys.settrace(None); c.close()
