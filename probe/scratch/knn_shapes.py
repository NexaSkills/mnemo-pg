# KNN rewrite battery: all operand shapes must execute or abstain loudly, never leak MATCH.
import sys
sys.path.insert(0,'/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
VEC = "[0.1,0.2,0.3]"
import json
q1 = f"SELECT rowid, distance FROM vec_working WHERE embedding MATCH vec_quantize_int8('{VEC}', \"unit\") AND k=5 ORDER BY distance"
q2 = f"SELECT rowid, distance FROM vec_working WHERE embedding MATCH ? AND k=5 ORDER BY distance"
q3 = f"SELECT rowid, distance FROM vec_working WHERE embedding MATCH vec_quantize_binary(?) AND k=5 ORDER BY distance"
q4 = f"SELECT rowid, distance FROM vec_working WHERE embedding MATCH '{VEC}' AND k=5 ORDER BY distance"
q5 = f"SELECT rowid FROM vec_working WHERE embedding MATCH vec_quantize_int8(?, \"unit\") AND k=? ORDER BY distance LIMIT ?"
for name, q, p in [("int8-literal",q1,()), ("bare-?",q2,("[0.1,0.2,0.3]",)),
                   ("binary-?",q3,("[0.1,0.2,0.3]",)), ("fp32-literal",q4,()),
                   ("int8-? lim-binds",q5,("[0.1,0.2,0.3]",5,5))]:
    try:
        rows = c.execute(q, p).fetchall() if p else c.execute(q).fetchall()
        print(f"{name}: OK {len(rows)} rows")
    except Exception as e:
        print(f"{name}: ERR {type(e).__name__}: {str(e)[:120]}")
c.close()
