# Direct replication of body wrapper logic on the dropped short SQL.
import sys, re
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()

short = "SELECT rowid, distance FROM vec_episodes WHERE embedding MATCH ? AND k=5"
d = nc._drop_companion_blob_projections(short)
print("d:", repr(d))
m = nc._VEC_KNN_RE.search(d)
print("m.end:", m.end(), "len:", len(d))
head = d[:m.start()].rstrip()
tail = d[m.end():].lstrip()
print("head:", repr(head))
print("tail:", repr(tail))
# operand from the body path
operand = (m.group("qargs") or m.group("op") or "").split(",")[0].strip()
print("operand:", repr(operand))
# m2 path (line 1466+ in-place generic): joined
cos = "embedding <=> " + operand
def _dist_ref(mm):
    pre = mm.string[:mm.start()]
    if re.search(r"[\w.]\s*$|\bAS\s*$", pre, re.I):
        return mm.group(0)
    return cos
joined = re.sub(r"\bdistance\b", _dist_ref, d)
m2 = nc._VEC_KNN_RE.search(joined)
if m2:
    head2 = joined[:m2.start()].rstrip()
    tail2 = joined[m2.end():].lstrip()
    print("m2 span:", m2.span())
    print("head2:", repr(head2))
    print("tail2:", repr(tail2))
