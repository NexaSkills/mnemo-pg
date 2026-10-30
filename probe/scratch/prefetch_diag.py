#!/usr/bin/env python3
"""Prefetch diagnosis: run provider.prefetch() with logger.debug visible so the
swallowed exception is exposed, then with the offending source disabled."""
import sys, logging, os
logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
                    format="PREFETCHDIAG %(levelname)s %(name)s %(message)s")
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import plugin  # noqa: F401
import hermes_memory_provider as hmp

ProvCls = getattr(hmp, "MnemosyneMemoryProvider", None) or hmp.MemoryProvider
prov = ProvCls()
prov.initialize("rankprobe")

out = prov.prefetch("ZEBRA99 ranking probe", session_id="")
print("PREFETCH_LEN:", len(out))
print(out[:300])
