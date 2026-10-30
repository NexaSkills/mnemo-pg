
import sys
sys.path.insert(0, "/home/mybuddy/workspace/mnemo-pg/repo")
import warnings; warnings.filterwarnings("ignore")
import plugin
from mnemo_backends import factory, native_conn as nc
c = factory.connect("/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db")
c.execute("SELECT 1").fetchall()
prev = [None]
def tracer(frame, event, arg):
    code = frame.f_code
    if "native_conn" not in code.co_filename: return None
    if code.co_name != "execute": 
        return None
    def localtr(f, e, a):
        if e == "line" and f is frame:
            s = f.f_locals.get("sql_s")
            if s and "vec_working" in s and "MATCH" not in s:
                if prev[0] != s:
                    print(f"L{f.f_lineno}: {s[-50:]!r}")
                    prev[0] = s
        return localtr
    return localtr
q = "SELECT rowid, distance FROM vec_working WHERE embedding MATCH ? AND k=5 ORDER BY distance"
sys.settrace(tracer)
try:
    c.execute(q, ("[0.1,0.2,0.3]",)).fetchall()
except Exception as ex:
    print("err:", type(ex).__name__, str(ex)[:100])
finally:
    sys.settrace(None); c.close()
