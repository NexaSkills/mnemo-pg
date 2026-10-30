#!/bin/bash
# poly x session matrix: fresh remember + recall, marker must rank.
set -u
PY=/home/mybuddy/.hermes/installs/9527c3f31db6a735/environments/8c570d1568234fd48f0d2908e8487d5a/venv/bin/python
for sess in default matrix$$; do
  for poly in 0 1; do
    MARK="MTX${sess: -6}$poly"
    out=$(MARK="$MARK" HERMES_HOME=/home/mybuddy/.hermes/profiles/mike MNEMO_PROFILE=mike \
      HERMES_SESSION_ID="$sess" MNEMOSYNE_POLYPHONIC_RECALL=$poly \
      timeout 120 "$PY" -I -c '
import os, sys
sys.path.insert(0, "/home/mybuddy/workspace/mnemo-pg/repo")
import plugin
from mnemosyne import Mnemosyne
mark = os.environ["MARK"]
eng = Mnemosyne()
eng.remember("matrix marker " + mark + ", whiskey barrel aging chemistry", importance=0.9, source="fact")
hits = eng.recall("whiskey barrel aging chemistry", top_k=5)
rank = next((i+1 for i,h in enumerate(hits) if mark in (h.get("content") or "")), None)
print("hits:", len(hits), "rank:", rank)
' 2>&1 | grep -E "^hits:")
    echo "session=$sess poly=$poly -> $out"
  done
done
