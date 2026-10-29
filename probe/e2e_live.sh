#!/usr/bin/env bash
# e2e_live.sh — real-engine write + recall probe against a profile's PG schema.
# Usage: PROFILE=mike bash e2e_live.sh
set -u
R=$(cd "$(dirname "$0")/.." && pwd)
PROFILE="${PROFILE:-mike}"
HOME_DIR="$HOME/.hermes"
[[ "$PROFILE" != "default" ]] && HOME_DIR="$HOME/.hermes/profiles/$PROFILE"
MARK="LIVEMARK$(date +%s)"
echo "MARK=$MARK PROFILE=$PROFILE"
export MNEMO_PROBE_MARK="$MARK" MNEMO_PROBE_HOME="$HOME_DIR"

cat > /tmp/e2e_engine_$PROFILE.py <<'PY'
import os
os.environ["HERMES_HOME"]=os.environ["MNEMO_PROBE_HOME"]
os.environ["MNEMOSYNE_POLYPHONIC_RECALL"]="1"
mark=os.environ["MNEMO_PROBE_MARK"]
import mnemo_backends.install as _i
_i.install()
from mnemosyne.core.memory import Mnemosyne
eng=Mnemosyne(db_path=os.environ["MNEMO_PROBE_HOME"]+"/mnemosyne/data/mnemosyne.db")
mid=eng.remember("GIRAFFE7 MARKER "+mark+" tall spotted african mammal neck", importance=0.9)
print("remembered:", mid)
res=eng.recall("GIRAFFE7 "+mark, top_k=5)
print("recall_hits:", len(res))
for r in res[:3]:
    c=(r.get("content") or r.get("text") or "")[:70]
    print("  id=%s score=%s %r" % (str(r.get("id","?"))[:16], r.get("score"), c))
PY

cat > /tmp/e2e_pg_$PROFILE.py <<'PY'
import psycopg
c=psycopg.connect(host="/home/mybuddy/lcm-pg/run",port=54329,dbname="lcm",user="lcm01")
c.execute("SET search_path TO mnemosyne_"+os.environ.get("MNEMO_PROBE_PROFILE","mike")+", public")
cur=c.execute("SELECT id FROM working_memory WHERE content LIKE %s", ("%"+os.environ["MNEMO_PROBE_MARK"]+"%",))
print("in_pg_ids:", [r[0][:16] for r in cur.fetchall()])
c.close()
PY

export MNEMO_PROBE_PROFILE="$PROFILE"
PYTHONPATH="$R" python3 /tmp/e2e_engine_$PROFILE.py
echo done
