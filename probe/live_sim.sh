#!/bin/bash
# Live-simulation: recall a fresh marker under POLY=1 (gateway's live env:
# .env sets POLY=1; the poly-off drop-in is only in unit env, Hermes
# re-reads profile .env per process). Profiles as args (default: mike).
set -u
PY=/home/mybuddy/.hermes/installs/9527c3f31db6a735/environments/8c570d1568234fd48f0d2908e8487d5a/venv/bin/python
[ -x "$PY" ] || PY=python3
REPO=/home/mybuddy/workspace/mnemo-pg/repo
for p in "${@:-mike}"; do
  TS=$(date +%s%N)
  MARK="LIVESIM${TS}_${p//[^a-z0-9]/}"
  out=$(MARK="$MARK" HERMES_HOME=/home/mybuddy/.hermes/profiles/$p MNEMO_PROFILE=$p \
    MNEMOSYNE_POLYPHONIC_RECALL=1 \
    timeout 120 "$PY" -I -c '
import os, sys
sys.path.insert(0, "/home/mybuddy/workspace/mnemo-pg/repo")
import plugin
from mnemosyne import Mnemosyne
mark = os.environ["MARK"]
eng = Mnemosyne()
mid = eng.remember("live sim marker " + mark + ": coffee brewing temperatures guide", importance=0.9)
hits = eng.recall("coffee brewing temperatures", top_k=10)
rank = next((i+1 for i,h in enumerate(hits) if mark in (h.get("content") or "")), None)
print("mid:", mid, "hits:", len(hits), "marker_rank:", rank)
' 2>&1 | grep -vE "sentence_transformers|Warning|warn" | tail -2)
  echo "$p: $out"
done
