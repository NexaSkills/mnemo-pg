# Trace WHY _rewrite_vec_knn_impl abstains on literal KNN shapes.
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
from mnemo_backends import native_conn as nc

VEC = "[0.1,0.2,0.3]"
q = f"SELECT rowid, distance FROM vec_working WHERE embedding MATCH '{VEC}' AND k=5 ORDER BY distance"
m = nc._VEC_KNN_RE.search(q)
print("companion_is_vector('vec_working') =", nc._companion_is_vector('vec_working'))
lit = m.group("lit")
print("lit group:", repr(lit))
print("_to_vec_operand(lit) =", repr(nc._to_vec_operand(lit)))
print("_LITERAL_VECTOR_RE.match(lit) =", bool(nc._LITERAL_VECTOR_RE.match(lit)) if hasattr(nc,'_LITERAL_VECTOR_RE') else 'NO ATTR')
print("has _fp32_operand_from_list:", hasattr(nc, '_fp32_operand_from_list'))
print("has _KNN_CALL_ONLY_RE:", hasattr(nc, '_KNN_CALL_ONLY_RE'), "| _ARG_SPLIT_RE:", hasattr(nc,'_ARG_SPLIT_RE'))
print("_PLACEHOLDER_RE.match(lit):", bool(nc._PLACEHOLDER_RE.match(lit)))
# what does _to_vec_operand accept? print it
import inspect
print(inspect.getsource(nc._to_vec_operand))
