# Which function sees tail == '5'?  Trace ALL frames with tail-like locals.
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()

q = "SELECT rowid, distance FROM vec_episodes WHERE embedding MATCH ? AND k=5"

def tracer(frame, event, arg):
    if "site-packages" in frame.f_code.co_filename:
        return None
    if event == "line" and frame.f_lineno == frame.f_code.co_firstlineno:
        pass
    if event == "return":
        loc = frame.f_locals
        if "'5'" == repr(loc.get("tail")) or repr(loc.get("tail")) == "'5'":
            print(f"TAIL5 in {frame.f_code.co_name} {frame.f_code.co_filename.split('/')[-1]}:{frame.f_lineno}")
    return tracer

sys.settrace(tracer)
try:
    out = nc._rewrite_vec_knn_body(q)
    print("FINAL:...", out[-26:])
finally:
    sys.settrace(None)
