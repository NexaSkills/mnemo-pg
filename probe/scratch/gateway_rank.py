#!/usr/bin/env python3
"""Ranking probe through the live provider surface (same call path as gateway tools).
Writes a fresh marker via mnemosyne_remember, then checks where mnemosyne_recall ranks it."""
import sys, uuid, json
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import plugin  # noqa: F401  loader: shadow + psycopg site + bind

import hermes_memory_provider as hmp

ProvCls = getattr(hmp, "MnemosyneMemoryProvider", None) or hmp.MemoryProvider
prov = ProvCls()
try:
    prov.initialize("rankprobe")
except Exception as e:
    print("init:", type(e).__name__, str(e)[:120])

marker = "GHOSTRANK " + uuid.uuid4().hex[:8]
out = prov.handle_tool_call("mnemosyne_remember", {
    "content": marker + " ZEBRA99 — ranking probe entry for recall ordering test.",
    "importance": 0.9,
})
print("remember:", str(out)[:160])

res = prov.handle_tool_call("mnemosyne_recall", {"query": "ZEBRA99", "limit": 8})
try:
    data = json.loads(res) if isinstance(res, str) else res
except Exception:
    print("RAW:", str(res)[:400]); sys.exit(0)
hits = data.get("results") or data.get("hits") or []
rank = None
for i, h in enumerate(hits):
    text = (h.get("content") or h.get("text") or "") if isinstance(h, dict) else str(h)
    if marker in text:
        rank = i + 1
        break
print("hits:", len(hits), "marker_rank:", rank)
for i, h in enumerate(hits[:5]):
    if isinstance(h, dict):
        print(f"  {i+1}. score={h.get('score')} text={str(h.get('content') or '')[:60]!r}")
