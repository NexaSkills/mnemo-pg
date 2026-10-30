# Full Cursor-level pipeline trace for the real core shapes.
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import warnings; warnings.filterwarnings('ignore')
import plugin
from mnemo_backends import native_conn as nc

class FakeCur:
    _schema = "mnemosyne_mike"
    _rewrite = nc.Cursor._rewrite
    _drop_companion_blob_projections = nc._drop_companion_blob_projections

SHAPES = {
    "fp32+emb":  ("SELECT rowid, distance, embedding FROM vec_episodes WHERE embedding MATCH ? AND k=5 ORDER BY distance", [None]),
    "int8+emb":  ("SELECT rowid, distance, embedding FROM vec_episodes WHERE embedding MATCH vec_quantize_int8(?) AND k=5 ORDER BY distance", [None]),
    "bit+emb":   ("SELECT rowid, distance, embedding FROM vec_episodes WHERE embedding MATCH vec_quantize_binary(?) AND k=5 ORDER BY distance", [None]),
    "fp32 only": ("SELECT rowid, distance FROM vec_episodes WHERE embedding MATCH ? AND k=5 ORDER BY distance", [None]),
}
for name, (q, binds) in SHAPES.items():
    c = FakeCur()
    out, _ = FakeCur._rewrite(c, q, binds)
    print(f"{name}:\n  {out}")
    try:
        nc._exec_direct(out) if hasattr(nc, '_exec_direct') else None
    except Exception:
        pass
