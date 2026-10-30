# Dump body value just before the return at 1522 (generic epilogue).
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
    if event == "line" and 1510 <= frame.f_lineno <= 1525:
        b = frame.f_locals.get("body")
        if b is not None:
            print(f"L{frame.f_lineno} body=...{str(b)[-30:]!r} LIMIT?" ,
                  bool(__import__('re').search(r'\bLIMIT\b', b, 2)))
    return tracer

sys.settrace(tracer)
try:
    out = nc._rewrite_vec_knn(q)
    print("FINAL:...", out[-40:])
finally:
    sys.settrace(None)
