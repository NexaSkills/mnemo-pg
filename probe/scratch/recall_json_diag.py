#!/usr/bin/env python3
"""Where does the recall tool's json.dumps fail? Re-run beam.recall via the
tool handler and serialize step by step."""
import sys, json, datetime
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import plugin  # noqa: F401
import hermes_memory_provider as hmp

ProvCls = getattr(hmp, "MnemosyneMemoryProvider", None) or hmp.MemoryProvider
prov = ProvCls()
prov.initialize("rankprobe")

res = prov._beam.recall("ZEBRA99", top_k=8)
print("beam.recall ok:", len(res))
for i, r in enumerate(res):
    bad = {k: (type(v).__name__, repr(v)[:40]) for k, v in r.items()
           if isinstance(v, (datetime.datetime, datetime.date, datetime.time, bytes))}
    print(f"hit{i} keys_with_nonjson={bad}")
try:
    json.dumps({"results": res})
    print("json.dumps OK without default=str")
except TypeError as e:
    print("json.dumps fails without default=str:", e)
try:
    json.dumps({"results": res}, default=str)
    print("json.dumps OK with default=str")
except TypeError as e:
    print("json.dumps STILL fails with default=str:", e)
