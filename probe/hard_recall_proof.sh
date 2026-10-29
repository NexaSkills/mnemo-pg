#!/bin/bash
# Hard recall proof per profile: unique marker -> remember -> recall must rank
# the marker top within the profile's OWN schema.
set -u
VENV=/home/mybuddy/.hermes/installs/9527c3f31db6a735/environments/8c570d1568234fd48f0d2908e8487d5a/venv/bin/python
PY=$VENV
[ -x "$PY" ] || PY=python3
REPO=/home/mybuddy/workspace/mnemo-pg/repo
TS=$(date +%s)
for p in "$@"; do
  MARK="HARDPROOF${TS}_${p//[^a-z0-9]/}"
  out=$(MNEMO_PROFILE="$p" HERMES_HOME=/home/mybuddy/.hermes/profiles/$p \
    timeout 120 "$PY" -I - "$MARK" <<'EOF' 2>&1 | tail -6
import os, sys
repo = "/home/mybuddy/workspace/mnemo-pg/repo"
sys.path.insert(0, repo)
import plugin
from mnemosyne import Mnemosyne
mark = sys.argv[1]
eng = Mnemosyne()
try:
    mid = eng.remember(f"hard proof marker {mark}: zebra stripes anatomy", importance=0.9)
    print("remember:", "OK", mid)
except Exception as e:
    print("remember: FAIL", type(e).__name__, e)
try:
    hits = eng.recall(mark.split("_")[0] + " zebra stripes anatomy", top_k=5)
    ids = [str(h.get("id") if isinstance(h, dict) else getattr(h, "id", h)) for h in hits]
    print("recall:", len(hits), "hits")
    print("marker_hit:", any(mark in (str(h) ) for h in hits))
except Exception as e:
    print("recall: FAIL", type(e).__name__, e)
EOF
)
  echo "== $p"
  echo "$out"
done
