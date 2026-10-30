# Which function returns 't 5'? Trace ALL functions in nc module.
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()

q = "SELECT rowid, distance FROM vec_episodes WHERE embedding MATCH ? AND k=5 ORDER BY distance"

def tracer(frame, event, arg):
    fn = frame.f_code.co_filename
    if "native_conn" not in fn:
        return tracer  # keep tracing children
    if event == "return" and isinstance(arg, str) and arg.rstrip().endswith("t 5"):
        print(f"LEAK in {frame.f_code.co_name}@{frame.f_lineno}: ...{arg[-24:]!r}")
        return None
    return tracer

sys.settrace(tracer)
try:
    out = nc._rewrite_vec_knn(q)
    print("FINAL:...", out[-30:])
finally:
    sys.settrace(None)
