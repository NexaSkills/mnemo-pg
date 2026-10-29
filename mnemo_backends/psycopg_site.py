"""psycopg_site — shared helper: make psycopg + psycopg_pool importable.

Scripts (check_all.py etc.) and the plugin loader both need psycopg even when
the invoking interpreter lacks it (e.g. fleet hosts keep psycopg only in the
lcm-pg private venv). Append candidate site-packages dirs (NEVER prepend, so
the host interpreter's own packages win) until BOTH modules import.
"""
from __future__ import annotations

import glob
import importlib
import os
import sys


def _both_ok() -> bool:
    try:
        importlib.import_module("psycopg")
        importlib.import_module("psycopg_pool")
        return True
    except Exception:
        return False


def candidates() -> list[str]:
    home = os.path.expanduser("~")
    cands = []
    # env-provided overrides first (fleet installs can point elsewhere)
    extra = os.environ.get("MNEMO_PSYCOPG_SITE", "").strip()
    if extra:
        cands.extend(p for p in extra.split(os.pathsep) if p)
    # lcm-pg private venvs (HOME layout and ~/.hermes layout)
    for root in (os.path.join(home, "lcm-pg", "venv"),
                 os.path.join(home, ".hermes", "lcm-pg", "venv"),
                 os.path.join(home, ".hermes", "mnemo-pg", "venv")):
        cands.extend(sorted(glob.glob(os.path.join(root, "lib", "python3.*", "site-packages"))))
    return cands


def ensure_psycopg(verbose: bool = False) -> bool:
    """Return True once psycopg AND psycopg_pool import in THIS interpreter."""
    if _both_ok():
        return True
    for sp in candidates():
        if not os.path.isdir(sp) or sp in sys.path:
            continue
        sys.path.append(sp)
        if _both_ok():
            if verbose:
                print(f"[psycopg_site] using {sp}")
            return True
        sys.path.remove(sp)
    return False
