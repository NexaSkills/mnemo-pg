# Dump m/kws/m.end/kw.end for the truncated shape.
import sys, re
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()

q = "SELECT rowid, distance FROM vec_episodes WHERE embedding MATCH ? AND k=5"
m = nc._VEC_KNN_RE.search(q)
print("m span:", m.span(), repr(m.group(0)))
print("groups:", {k: m.group(k) for k in m.groupdict()})
kw = nc._VEC_KNN_AND_K_RE.search(q, m.start())
print("kw span:", kw.span(), repr(kw.group(0)))
print("len(q):", len(q), "m.end:", m.end(), "kw.end:", kw.end())
