#!/usr/bin/env python3
"""Recall check under the gateway's EXACT boot environment.

Usage: probe/gateway_recall.py <profile> <marker>
Mimics the systemd units: isolated -I launcher, canonical install
site-packages on PYTHONPATH, HERMES_HOME=profile, mnemosyne plugin
resolved from the profile's plugins/ dir (now a symlink to the
mnemo-pg loader). Writes+recalls a marker through the real engine
with the default db path (no db_path override) — i.e. exactly the
surface the gateway uses.
"""
import os, sys

PROFILE = sys.argv[1] if len(sys.argv) > 1 else "mike"
MARKER = sys.argv[2] if len(sys.argv) > 2 else "ZEBRA99"

os.environ["HERMES_HOME"] = os.path.expanduser(f"~/.hermes/profiles/{PROFILE}")
os.environ.setdefault("MNEMO_BACKEND", "postgres")

import importlib.util
import types

loader = os.path.expanduser(f"~/.hermes/profiles/{PROFILE}/plugins/mnemosyne/__init__.py")
if not os.path.exists(loader):
    print("FAIL: loader missing at", loader)
    raise SystemExit(1)
spec = importlib.util.spec_from_file_location(
    "mnemosyne", loader, submodule_search_locations=[os.path.dirname(loader)]
)
mod = types.ModuleType("mnemosyne")
mod.__file__ = loader
mod.__path__ = [os.path.dirname(loader)]
sys.modules["mnemosyne"] = mod
with open(loader) as fh:
    exec(compile(fh.read(), loader, "exec"), mod.__dict__)

import mnemosyne.core.beam as beam  # noqa: E402

print("BEAM:", "VENDOR" if "mnemo-pg" in beam.__file__ else "SITE", beam.__file__)

from mnemosyne import Mnemosyne  # noqa: E402

eng = Mnemosyne()  # default db path, like the gateway
try:
    c = eng._connect() if hasattr(eng, "_connect") else eng.conn
    ok = c.execute("SELECT pg_backend_pid() > 0").fetchone()[0]
    print("ROUTE:", "PG" if ok else "SQLITE")
except Exception as exc:
    print("ROUTE_ERR:", repr(exc)[:120])

mid = eng.remember(f"{MARKER} switchover probe {PROFILE}", source="conversation",
                   importance=0.9, extract_entities=True)
print("MARKER:", mid)
hits = eng.recall(MARKER, top_k=5)
print("RECALL_HITS:", len(hits), "ids:", [h["id"] for h in hits[:3]])

import psycopg  # noqa: E402

schema = "mnemosyne_" + (PROFILE if PROFILE != "default" else "default")
conn = psycopg.connect(host=os.path.expanduser("~/lcm-pg/run"), port=54329,
                       dbname="lcm", user="lcm01")
conn.execute(f'SET search_path TO "{schema}"')
n = conn.execute("SELECT count(*) FROM working_memory WHERE content LIKE %s",
                 (f"%{MARKER}%",)).fetchone()[0]
print(f"IN_PG_{schema}:", n)
