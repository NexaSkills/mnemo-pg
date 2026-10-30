# Real Cursor.execute with traced rewrite pipeline, isolated probe process.
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import factory, native_conn as nc

c = factory.connect('/home/mybuddy/.hermes/profiles/mike/mnemosyne/data/mnemosyne.db')
c.execute("SELECT 1").fetchall()
print("schema in effect:", nc._CURRENT_SCHEMA[0])
print("companion:", nc._companion_is_vector("vec_episodes"))

q = "SELECT rowid, distance, embedding FROM vec_episodes WHERE embedding MATCH ? AND k=5 ORDER BY distance"

# replicate the Cursor passes in order
s = nc._normalize_ddl(q)
print("1 normalize_ddl:", s[:100])
s2 = nc._rewrite_upsert(s, c)
s3, ch = nc._fts_rewrite.rewrite_fts_sql(s2) if hasattr(nc, '_fts_rewrite') else (s2, False)
print("2 fts_rewrite:", s3[:130] if s3 != s2 else "<ident>")
k = nc._rewrite_vec_knn(s3)
print("3 knn:", "<ident>" if k is s3 else k)
try:
    native, bound = nc._params.render(k, [None], conn=c)
    print("4 render:", native[:130])
except Exception as e:
    print("4 render ERR:", e)
