#!/usr/bin/env python3
"""Direct prefetch internals: which sources, and what render_bank_source does raw."""
import sys
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
import plugin  # noqa: F401
import hermes_memory_provider as hmp

ProvCls = getattr(hmp, "MnemosyneMemoryProvider", None) or hmp.MemoryProvider
prov = ProvCls()
prov.initialize("rankprobe")

prof = hmp._resolve_profile(getattr(prov, "_prefetch_profile", None))
print("profile:", getattr(prof, "name", "?"), "sources:", getattr(prof, "sources", None))

# raw recall on the beam
try:
    res = prov._beam.recall("ZEBRA99", top_k=5)
    print("beam.recall:", len(res), [ (str(r.get('id'))[:8], r.get('score')) for r in res[:3] ])
except Exception as e:
    print("beam.recall FAILED:", type(e).__name__, str(e)[:200])

# render_bank_source raw
try:
    blk = hmp.render_bank_source(prov._beam, "ZEBRA99", "", prof,
                                 ledger=getattr(prov, "_verbatim_ledger", None),
                                 active_session_id="",
                                 lock=prov._ensure_beam_access_lock())
    print("render_bank_source len:", len(blk))
    print(blk[:200])
except Exception as e:
    import traceback; traceback.print_exc()

# identity + model slots
try:
    print("identity_rows:", len(hmp.identity_rows(prov._beam)))
except Exception as e:
    print("identity_rows FAILED:", type(e).__name__, str(e)[:200])
try:
    ms = hmp.render_model_slots(prov._beam, "ZEBRA99", prof, canonical_owner=prov._canonical_owner())
    print("model_slots len:", len(ms))
except Exception as e:
    print("model_slots FAILED:", type(e).__name__, str(e)[:200])
