# Where does the '5' leak in the LATERAL wrapper path come from?
import sys, re, linecache
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

impl = nc._rewrite_vec_knn_impl

# tracer: log every line executing in the impl via sys.settrace
import threading
src_lines = {}
import inspect
src = inspect.getsource(impl).splitlines()
def tracer(frame, event, arg):
    if frame.f_code.co_name != "_rewrite_vec_knn_impl":
        return None
    if event == "line":
        ln = frame.f_lineno
        try:
            txt = frame.f_code.co_firstlineno and None
        except Exception:
            pass
        print(f"  :{ln}:")
    elif event == "return":
        print(f"  RETURN {str(arg)[:120]}")
    return tracer

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
# warm the companion cache with one executed query first
c.execute("SELECT 1").fetchall()
q = "SELECT rowid, distance FROM vec_working WHERE embedding MATCH ? AND k=5 ORDER BY distance"
sys.settrace(tracer)
try:
    rows = c.execute(q, ("[0.1,0.2,0.3]",)).fetchall()
finally:
    sys.settrace(None)
    c.close()
