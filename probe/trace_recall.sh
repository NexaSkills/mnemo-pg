#!/usr/bin/env bash
# One-shot trace harness: run an engine recall with native_conn SQL tracing on.
# Usage: trace_recall.sh [profile] [query]
P=${1:-mike}
Q=${2:-GHOST42 zebra}
V=/home/mybuddy/.hermes/installs/9527c3f31db6a735/environments/8c570d1568234fd48f0d2908e8487d5a/venv/bin/python
cd ~/workspace/mnemo-pg/repo || exit 1
MNEMO_PG_TRACE_SQL=1 MNEMO_TRACE_VEC=1 HERMES_HOME=$HOME/.hermes/profiles/$P "$V" - <<PY
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo/vendor')
import plugin
from mnemosyne import Mnemosyne
eng = Mnemosyne()
hits = eng.recall("$Q", top_k=5)
print('hits', len(hits))
PY
