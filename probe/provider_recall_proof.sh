#!/bin/bash
# Provider-surface recall proof. Pass profile as arg 1 (default mike).
# IMPORTANT: run this from INSIDE the target profile's own hermes session
# (its session_id = the HERMES_SESSION_ID the gateway tools write under);
# cross-session session-scoped rows are invisible by design (#visibility).
# Env override HERMES_SESSION_ID=<id> to test another lane explicitly.
set -u
P=${1:-mike}
PY=/home/mybuddy/.hermes/installs/c3ba7568a6a82e98/environments/fed0c64f3812450e99e495966acd5eb9/venv/bin/python
HERMES_HOME=/home/mybuddy/.hermes/profiles/$P MNEMO_PROFILE=$P timeout 120 "$PY" - <<'PY' 2>/dev/null
import sys, uuid, os, json
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import plugin
import hermes_memory_provider as hmp
C = getattr(hmp, "MnemosyneMemoryProvider", None) or hmp.MemoryProvider
prov = C()
try: prov.initialize(os.environ.get("PROV_SESSION", "provproof"))
except Exception: pass
beam = prov._beam
marker = "PROOFMARK " + uuid.uuid4().hex[:8]
mid = beam.remember(marker + " zebra stripes anatomy provider proof", importance=0.9, source="fact")
res = prov.handle_tool_call("mnemosyne_recall", {"query": marker + " zebra stripes anatomy", "limit": 10})
data = json.loads(res)
hits = data.get("results") or []
rank = None
for i, h in enumerate(hits):
    if (isinstance(h, dict) and (h.get("id") == mid or h.get("memory_id") == mid)) or marker in (h.get("content") if isinstance(h, dict) else str(h)):
        rank = i + 1; break
print("session_id:", beam.session_id)
print("remember: OK  recall_hits:", len(hits), " marker_rank:", rank)
PY
