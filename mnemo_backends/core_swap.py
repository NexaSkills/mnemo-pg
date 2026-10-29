"""Route every vendored-core `sqlite3.connect` through the backend factory.

The vendored mnemosyne core does plain `import sqlite3` +
`sqlite3.connect(path)` at ~48 sites across 27 modules. Patching each call
site by hand is churn; a global stdlib monkeypatch is racy. Instead this
module provides a per-module proxy swap (same pattern as
sqlite3_compat.sqlite3_if_postgres) plus `apply_all()`, which walks
`sys.modules` and replaces the module-global `sqlite3` binding of every
vendored `mnemosyne.*` / `hermes_memory_provider*` module with the proxy.

Semantics preserved:
  * `:memory:` (and, via factory's placeholder heuristics, genuinely-empty
    store placeholders) still reach stdlib sqlite3 through the proxy — only
    real store paths route to PG.
  * attribute access (sqlite3.Row, sqlite3.Error, ...) passes through to the
    real stdlib module, so isinstance/except clauses are untouched.

Call apply_all() once, after the vendored core is imported (install.py's
bootstrap does this). Idempotent and cheap; on the sqlite backend it is a
guaranteed no-op.
"""

from __future__ import annotations

import sys
import types

from . import factory
from .sqlite3_compat import sqlite3_if_postgres, is_postgres

_VENDORED_PREFIXES = ("mnemosyne.", "hermes_memory_provider")

_SWAPPED = set()


def _swap_module(mod) -> bool:
    real = getattr(mod, "sqlite3", None)
    if real is None or not isinstance(real, types.ModuleType):
        return False
    if getattr(real, "_mnemo_proxy", False):
        return False  # already swapped
    proxy = sqlite3_if_postgres(real)
    if proxy is real:
        return False
    # tag a fresh proxy once (cache returns the same tagged object)
    setattr(real, "_mnemo_proxy_tag", True)
    try:
        mod.sqlite3 = proxy
    except Exception:
        return False
    _SWAPPED.add(mod.__name__)
    return True


def _is_vendored(mod) -> bool:
    name = getattr(mod, "__name__", "") or ""
    if name.startswith(_VENDORED_PREFIXES) or name in ("mnemosyne", "hermes_memory_provider"):
        return True
    file = getattr(mod, "__file__", "") or ""
    return "/mnemo-pg/" in file and "site-packages" not in file


def apply_all() -> int:
    """Swap `sqlite3` globals in every already-imported vendored module.

    Returns the number of modules swapped. No-op on the sqlite backend.
    """
    if not is_postgres():
        return 0
    n = 0
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if _is_vendored(mod) and _swap_module(mod):
            n += 1
    return n


def swap_if_needed(module) -> bool:
    """Hook for import callbacks: swap one module if it is vendored core."""
    if not is_postgres():
        return False
    if _is_vendored(module):
        return _swap_module(module)
    return False
