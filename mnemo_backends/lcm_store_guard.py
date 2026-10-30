"""LCM store files: convert-to-PG backstop for processes WITHOUT this plugin.

Jan directive 2026-10-30: no sqlite databases for LCM (or mnemosyne); any
call that would open/create one is CONVERTED to native PostgreSQL, never
suppressed.

Primary conversions already live:
  * mnemosyne stores: binding.py proxies + sqlite_to_pg backstop (this
    repo), proven live (store-named sqlite3.connect -> NativeConn).
  * LCM stores in agent/gateway processes: the lcm-pg fork swaps sqlite3
    per store module (backends/sqlite3_compat.py) when LCM_BACKEND=postgres.

Gap this closes: STANDALONE lcm-pg scripts (cron scripts, maintenance) run
via `python -s` with the lcm-pg venv site-packages on PYTHONPATH — outside
the plugin import tree, so neither the fork's per-module swap nor the
mnemosyne plugin's bootstrap executes, and their `sqlite3.connect(...)`
calls would create real lcm*.db sqlite files.

Mechanism: a sitecustomize-style usercustomize hook, injected via the
lcm-pg venv's site-packages/usercustomize.py by scripts/lcm_store_guard_install.py.
When LCM_BACKEND (or MNEMO_BACKEND) resolves to postgres, it wraps
sqlite3.connect with the SAME naming rule (lcm.db / lcm_*.db / .db under
an .../lcm.../ dir) and routes those connects to
mnemo_backends.factory.connect -> NativeConn on the lcm-pg cluster.
Everything else (:memory:, state.db, kanban.db, unrelated stores) is
untouched. Fail-open on any error so scripts never break at import.
"""
from __future__ import annotations

import os
import sqlite3
import sys

_installed = False


def _is_lcm_store(s: str) -> bool:
    if s == ":memory:" or (s.startswith("file:") and ":memory:" in s):
        return False
    plain = s[5:].split("?", 1)[0] if s.startswith("file:") else s
    base = os.path.basename(plain).lower()
    if "/backups/" in plain.replace("\\", "/"):
        return False
    if base == "lcm.db" or (base.startswith("lcm_") and base.endswith(".db")):
        return True
    if base.endswith(".db") and "/lcm" in plain.replace("\\", "/").lower():
        return True
    return False


def _pg_enabled() -> bool:
    v = (os.environ.get("LCM_BACKEND") or os.environ.get("MNEMO_BACKEND")
         or "").lower()
    return v in ("postgres", "postgresql", "pg")


def install() -> bool:
    global _installed
    if _installed or not _pg_enabled():
        return False
    try:
        import mnemo_backends.factory as factory  # repo on sys.path via launcher
    except Exception:
        try:
            repo = os.environ.get("MNEMO_REPO") or os.path.expanduser(
                "~/workspace/mnemo-pg/repo")
            if repo not in sys.path:
                sys.path.append(repo)
            import mnemo_backends.factory as factory
        except Exception:
            return False
    _orig_connect = sqlite3.connect

    def _convert_connect(path=":memory:", *args, **kwargs):
        s = str(path)
        try:
            if _is_lcm_store(s):
                return factory.connect(s, **kwargs)
        except Exception:
            raise RuntimeError(
                f"mnemo-pg: LCM store connect {s!r} could not convert to "
                "PostgreSQL (refusing sqlite fallback)") from None
        return _orig_connect(path, *args, **kwargs)

    _convert_connect.__name__ = "connect"
    try:
        sqlite3.connect = _convert_connect  # type: ignore[attr-defined]
        _installed = True
        return True
    except Exception:
        return False
