#!/bin/bash
# Fresh-marker proof under the REAL production flag set (profile .env).
# Inserts a unique marker and checks polyphonic recall ranks it.
# With the FTS-voice fix deployed, this must show marker IN on all voices-that-can-see + marker_rank set.
set -u
VENV=/home/mybuddy/.hermes/installs/9527c3f31db6a735/environments/8c570d1568234fd48f0d2908e8487d5a/venv/bin/python
PY=$VENV
[ -x "$PY" ] || PY=python3
REPO=/home/mybuddy/workspace/mnemo-pg/repo
TS=$(date +%s%N)
for p in "$@"; do
  MARK="FRESHPROOF${TS}_${p//[^a-z0-9]/}"
  out=$(MNEMO_PROFILE="$p" HERMES_HOME=/home/mybuddy/.hermes/profiles/$p \
    timeout 120 "$PY" -I - "$MARK" <<'EOF' 2>&1 | tail -8
import os, sys
repo = "/home/mybuddy/workspace/mnemo-pg/repo"
sys.path.insert(0, repo)
import plugin
from mnemosyne import Mnemosyne
mark = sys.argv[1]
eng = Mnemosyne()
try:
    mid = eng.remember(f"fresh proof marker {mark}: zebra stripes anatomy", importance=0.9)
    print("remember:", "OK", mid)
except Exception as e:
    print("remember: FAIL", type(e).__name__, e)
    raise SystemExit
# query WITHOUT temporal keywords — an ordinary user's query
hits = eng.recall(f"{mark} zebra stripes anatomy", top_k=10)
rank = next((i+1 for i,h in enumerate(hits) if (h.get("id") if isinstance(h, dict) else getattr(h,"id",None)) == mid), None)
print("recall:", len(hits), "hits; marker_rank:", rank)
EOF
)
  echo "== $p"
  echo "$out"
done
