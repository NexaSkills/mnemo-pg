import sys, re
sys.path.insert(0,'/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import native_conn as nc
from mnemosyne import Mnemosyne
eng = Mnemosyne()
print("schema:", nc._CURRENT_SCHEMA, flush=True)
q = "SELECT rowid, distance FROM vec_working WHERE embedding MATCH vec_quantize_int8('[0.1,0.2,0.3]', \"unit\") AND k=5 ORDER BY distance"
m = nc._VEC_KNN_RE.search(q)
fm = re.search(r"\bFROM\s+(?:\w+\s*\.\s*)?(\w+)", q, re.I)
print("companion:", nc._companion_is_vector(fm.group(1)), flush=True)
vo = None
for mm in nc._KNN_CALL_ONLY_RE.finditer(m.group(0)):
    a = mm.group(1) if mm.group(1) is not None else mm.group(2)
    first = re.split(nc._ARG_SPLIT_RE, a or "")[0].strip()
    print("first:", repr(first), flush=True)
    vo = nc._to_vec_operand(first)
    print("vo:", vo, flush=True)
out = nc._rewrite_vec_knn(q)
print("MATCH survives:", "MATCH" in out, flush=True)
print(out[:160], flush=True)
