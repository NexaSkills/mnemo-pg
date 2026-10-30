# Trace: WHERE does ' 5' get appended after _rewrite_vec_knn returns clean?
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()  # warm

q = "SELECT rowid, distance FROM vec_working WHERE embedding MATCH ? AND k=5 ORDER BY distance"

def tracer(frame, event, arg):
    code = frame.f_code
    if code.co_filename.endswith("native_conn.py"):
        if event == "call" and code.co_name in ("execute", "executemany", "executescript"):
            print(f"CALL {code.co_name} @{code.co_firstlineno}")
        if event == "line" and code.co_name == "execute":
            ln = frame.f_lineno
            if ln >= 1905:  # after knn rewrite in Cursor.execute (line ~1912)
                sql_s = frame.f_locals.get("sql_s")
                if sql_s and "vec_working" in str(sql_s) and "MATCH" not in str(sql_s):
                    print(f"  L{ln} sql_s ends: ...{str(sql_s)[-60:]!r}")
    return tracer

sys.settrace(tracer)
try:
    rows = c.execute(q, ("[0.1,0.2,0.3]",)).fetchall()
    print("rows:", len(rows))
except Exception as e:
    print("err:", type(e).__name__, str(e)[:150])
finally:
    sys.settrace(None)
    c.close()
