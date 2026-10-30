#!/usr/bin/env bash
# Round-trip recall probe: N fresh remembers + recalls on one profile.
# Uses the production entry path (import plugin + Mnemosyne()) exactly like fresh_recall_proof.sh.
# Usage: bash probe/roundtrip.sh [profile] [N]
set -u
PROFILE="${1:-mike}"; N="${2:-6}"
VENV=/home/mybuddy/.hermes/installs/9527c3f31db6a735/environments/8c570d1568234fd48f0d2908e8487d5a/venv/bin/python
PY=$VENV; [ -x "$PY" ] || PY=python3
for i in $(seq 1 "$N"); do
  MNEMO_PROFILE="$PROFILE" HERMES_HOME="$HOME/.hermes/profiles/$PROFILE" \
    timeout 120 "$PY" -I - <<'PY' 2>&1 | tail -2
import sys, time
sys.path.insert(0, "/home/mybuddy/workspace/mnemo-pg/repo")
import plugin
from mnemosyne import Mnemosyne
t = f"ZEBRA-{int(time.time()*1000)%1000000}"
eng = Mnemosyne()
eng.remember(f"memorise this probe marker {t}", importance=0.9, source="fact")
r = eng.recall("zebra probe marker " + t)
rank = next((k+1 for k, x in enumerate(r) if t in (x.get("content") or "")), None)
print(f"probe {t}: hits={len(r)} rank={rank}")
PY
done
