# Test the EXACT core shapes (embedding projection in SELECT list).
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()

V = "[0.1,0.2,0.3]"
shapes = [
 ("fp32+emb",  "SELECT rowid, distance, embedding FROM vec_episodes WHERE embedding MATCH ? AND k=5 ORDER BY distance", (V,)),
 ("int8+emb",  'SELECT rowid, distance, embedding FROM vec_episodes WHERE embedding MATCH vec_quantize_int8(?, "unit") AND k=5 ORDER BY distance', (V,)),
 ("bit+emb",   "SELECT rowid, distance, embedding FROM vec_episodes WHERE embedding MATCH vec_quantize_binary(?) AND k=5 ORDER BY distance", (V,)),
 ("fp32 only", "SELECT rowid, distance FROM vec_episodes WHERE embedding MATCH ? AND k=5 ORDER BY distance", (V,)),
]
def tracer(frame, event, arg):
    if frame.f_code.co_name != "_rewrite_vec_knn_impl": return None
    if event == "return":
        print(f"  RET@{frame.f_lineno} out={str(arg)[-40:]!r}")
    return tracer

for name, q, p in shapes:
    print("="*66, name)
    k = nc._rewrite_vec_knn(q)
    print("  rewrite:", "<IDENT>" if k is q else k)
    try:
        rows = c.execute(q, p).fetchall()
        print("  EXEC OK:", len(rows), "rows")
    except Exception as e:
        print("  EXEC ERR:", type(e).__name__, str(e)[:120])
c.close()
