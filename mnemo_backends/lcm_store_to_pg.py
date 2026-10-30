"""LCM store files: convert-to-PG backstop for the lcm-pg plugin loader.

Jan directive 2026-10-30: no sqlite databases for LCM; any call that would
open/create one is CONVERTED to native PostgreSQL, never suppressed.

The lcm-pg fork already swaps sqlite3 for a PG-routed module per store
module (backends/sqlite3_compat.py) whenever LCM_BACKEND=postgres — that
is the primary conversion, same design as mnemo-pg's binding.py.

This backstop wraps stdlib sqlite3.connect (installed by the mnemosyne
plugin bootstrap, which runs in every agent/gateway process that also
loads lcm-pg) so a connect issued OUTSIDE the fork's swapped modules —
a standalone script, a diagnostic, a stray fallback — that names an LCM
store (lcm.db, lcm_*.db, or a .db under an .../lcm.../ dir) goes through
mnemo_backends.factory.connect to the SAME PG cluster instead of creating
a sqlite file. Hermes' own stores (state.db, kanban.db, cron dbs) are
untouched; :memory: scratchpads stay stdlib sqlite.

LCM store paths never contain 'mnemosyne', so this cannot clash with the
mnemosyne-side routing; whichever shim is installed, the shared rule is
identical: store-named sqlite opens => PostgreSQL.
"""
from __future__ import annotations

import os
import sqlite3

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


def install() -> bool:
    global _installed
    if _installed:
        return False
    try:
        from . import factory
        if factory.backend_for() != "postgres":
            return False
    except Exception:
        return False

    # sqlite_to_pg already converts every mnemosyne-named store path.
    # Extend its router with the LCM naming rule (same behaviour: PG).
    from . import sqlite_to_pg
    if sqlite_to_pg._installed and not getattr(sqlite_to_pg, "_lcm_rule", False):
        _orig = sqlite_to_pg._is_store_path

        def _is_store_path(s: str) -> bool:
            return _orig(s) or _is_lcm_store(s)

        _is_store_path.__name__ = "_is_store_path"
        sqlite_to_pg._is_store_path = _is_store_path
        sqlite_to_pg._lcm_rule = True
        _installed = True
        return True

    # If sqlite_to_pg did not install (e.g. plugin boot order), install our
    # own minimal wrapper: LCM-named store connects convert to PG.
    _orig_connect = sqlite3.connect

    def _convert_connect(path=":memory:", *args, **kwargs):
        s = str(path)
        if _is_lcm_store(s):
            return factory.connect(s, *args, **kwargs)
        return _orig_connect(path, *args, **kwargs)

    _convert_connect.__name__ = "connect"
    try:
        sqlite3.connect = _convert_connect  # type: ignore[attr-defined]
        _installed = True
        return True
    except Exception:
        return False
