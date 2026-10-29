#!/usr/bin/env bash
# qsig.sh — quick recall-signals probe on a profile (poly ON).
set -u
R=$(cd "$(dirname "$0")/.." && pwd)
PROFILE="${PROFILE:-mike}"
HOME_DIR="$HOME/.hermes"
[[ "$PROFILE" != "default" ]] && HOME_DIR="$HOME/.hermes/profiles/$PROFILE"
MARK="QSIG$(date +%s)"
export MNEMO_PROBE_MARK="$MARK" MNEMO_PROBE_HOME="$HOME_DIR"
echo "MARK=$MARK"

cat > /tmp/qsig_$PROFILE.py <<'PY'
import os
os.environ["HERMES_HOME"]=os.environ["MNEMO_PROBE_HOME"]
os.environ["MNEMOSYNE_POLYPHONIC_RECALL"]="1"
mark=os.environ["MNEMO_PROBE_MARK"]
import mnemo_backends.install as _i
_i.install()
from mnemosyne.core import beam
from mnemosyne.core.memory import Mnemosyne
eng=Mnemosyne(db_path=os.environ["MNEMO_PROBE_HOME"]+"/mnemosyne/data/mnemosyne.db")
conn=eng.conn
eng.remember("PLATYPUS9 MARKER "+mark+" duck billed monotreme", importance=0.9)
q="PLATYPUS9 "+mark
try:
    hits=beam.polyphonic_recall(conn,q,top_k=5); print("POLY:", len(hits))
    for h in hits[:3]: print("  ", str(h.get("id"))[:16], round(h.get("score",0),3), repr((h.get("content") or "")[:50]))
except Exception as e: print("POLY_ERR:", type(e).__name__, str(e)[:100])
try:
    ws=beam._wm_vec_search(conn,q,k=5); print("WM_VEC:", ws[:2])
except Exception as e: print("WM_VEC_ERR:", type(e).__name__, str(e)[:120])
try:
    f=beam._fts_rows(conn,q,5); print("FTS:", [str(r[0])[:12] for r in f])
except Exception as e: print("FTS_ERR:", type(e).__name__, str(e)[:120])
try:
    print("SLEEP:", eng.sleep(force=True))
except Exception as e: print("SLEEP_ERR:", type(e).__name__, str(e)[:140])
import sys; sys.stderr.flush()
PY

PYTHONPATH="$R" python3 /tmp/qsig_$PROFILE.py 2>&1 | grep -vE "sentence_transformers|warnings|^\s*$" | tail -14
echo done
