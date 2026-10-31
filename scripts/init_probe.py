#!/usr/bin/env python3
"""Live-init probe for one profile: real provider init against the live schema.

Runs the vendored core's BeamMemory init exactly as the provider does
(same import order, same schema resolution via MNEMO_PG_SCHEMA from the
profile home), then reports ok/err. This is the host-level equivalent of
mnemosyne_stats: init ok => provider tools would register.
"""
import sys, os, pathlib

profile = sys.argv[1] if len(sys.argv) > 1 else "default"
home = (pathlib.Path.home() / ".hermes/profiles" / profile) if profile != "default" else (pathlib.Path.home() / ".hermes")
os.environ["HERMES_HOME"] = str(home)
sys.path.insert(0, str(pathlib.Path.home() / "workspace/mnemo-pg/repo"))
sys.path.insert(0, str(pathlib.Path.home() / "workspace/mnemo-pg/repo/vendor"))
from mnemo_backends import binding
binding.bind()

try:
    from mnemosyne.core.beam import BeamMemory
    b = BeamMemory(session_id="probe_" + profile, db_path=str(home / "mnemosyne/data/mnemosyne.db"))
    b.remember("live init probe", importance=0.1)
    conn = getattr(b, "_conn", None) or getattr(b, "conn", None)
    cls = type(conn).__name__
    print(f"INIT {profile}: ok conn={cls}")
except Exception as e:
    print(f"INIT {profile}: ERR {str(e)[:130]}")
