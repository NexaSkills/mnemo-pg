#!/usr/bin/env bash
# parity_check.sh — SQLite-vs-PG recall parity + recall-signals for a profile.
# Usage: PROFILE=mike bash parity_check.sh
set -u
R=$(cd "$(dirname "$0")/.." && pwd)
PROFILE="${PROFILE:-mike}"
HOME_DIR="$HOME/.hermes"
[[ "$PROFILE" != "default" ]] && HOME_DIR="$HOME/.hermes/profiles/$PROFILE"
export MNEMO_PARITY_HOME="$HOME_DIR" MNEMO_PARITY_PROFILE="$PROFILE"

cat > /tmp/parity_$PROFILE.py <<'PY'
import os
home=os.environ["MNEMO_PARITY_HOME"]; prof=os.environ["MNEMO_PARITY_PROFILE"]
os.environ["HERMES_HOME"]=home
os.environ["MNEMOSYNE_POLYPHONIC_RECALL"]="1"
MARK="PARITY"+str(int(__import__("time").time()))
db=home+"/mnemosyne/data/mnemosyne.db"

def run(mode):
    if mode=="pg":
        import mnemo_backends.install as _i; _i.install()
    from mnemosyne.core.memory import Mnemosyne
    eng=Mnemosyne(db_path=db)
    eng.remember("GIRAFFE MARKER "+MARK+" tall spotted african mammal neck", importance=0.9)
    res=eng.recall("GIRAFFE "+MARK, top_k=8)
    ids=[str(r.get("id",""))[:16] for r in res]
    print(mode, "hits=", len(res), "ids=", ids[:4])
    return set(ids)

os.environ["MNEMO_BACKEND"]="sqlite"; a=run("sqlite")
os.environ["MNEMO_BACKEND"]="postgres"; b=run("pg")
print("overlap:", len(a&b), "sqlite_only:", sorted(a-b)[:4], "pg_only:", sorted(b-a)[:4])
PY

PYTHONPATH="$R" python3 /tmp/parity_$PROFILE.py 2>/tmp/parity_err_$PROFILE.log
echo "rc=$?"
grep -cE "Traceback" /tmp/parity_err_$PROFILE.log | sed 's/^/tracebacks_in_log: /'
echo done
