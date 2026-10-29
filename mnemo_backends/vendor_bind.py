"""Bind the vendored mnemosyne core onto the translation backend.

Importing this module (before the mnemosyne core is first imported) makes
every ``sqlite3.connect`` inside the vendored core route through
``mnemo_backends.factory``:

* ``mnemosyne.*`` and ``hermes_memory_provider.*`` modules get their module
  global ``sqlite3`` swapped to the compat proxy — the same mechanism the
  provider core uses for its own overrides (``sqlite3_if_postgres``).
* The stdlib ``sqlite3`` module itself is hooked too (path-aware: real
  on-disk SQLite files and ``:memory:`` still go to the real engine), so
  late imports and standalone scripts are covered.

Backend selection stays entirely in ``factory`` (MNEMO_BACKEND env; PG by
default). Nothing here decides a backend on its own.
"""

from __future__ import annotations

import sys

BOUND = False
_ERRORS: list = []


def bind_vendor() -> bool:
    """Swap sqlite3 inside vendored mnemosyne modules; install the global hook.

    Idempotent. Returns True when the global stdlib hook is active (the
    per-module swap is only meaningful when the PG backend is selected;
    under the sqlite backend the proxies pass everything through).
    """
    global BOUND
    if BOUND:
        return True
    try:
        from . import sqlite3_compat
    except Exception as exc:  # pragma: no cover
        _ERRORS.append(f"backends unavailable: {exc}")
        return False
    import importlib as _il
    _install_hook = _il.import_module(__package__ + ".install")  # package attr "install" is a function

    import sqlite3 as _sqlite3
    proxy = sqlite3_compat.sqlite3_if_postgres(_sqlite3)
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if name == "mnemosyne" or name.startswith("mnemosyne."):
            if getattr(mod, "sqlite3", None) is proxy:
                continue
            if hasattr(mod, "__dict__") and "sqlite3" in getattr(mod, "__dict__", {}):
                try:
                    setattr(mod, "sqlite3", proxy)
                except Exception as exc:  # pragma: no cover
                    _ERRORS.append(f"{name}: {exc}")
    try:
        _install_hook.install_stdlib_connect_hook()
    except Exception as exc:  # pragma: no cover
        _ERRORS.append(f"stdlib hook: {exc}")
        return False
    _rewrite_vec_modules()
    BOUND = True
    return True


def _rewrite_vec_modules() -> None:
    """Pre-rewrite vec0 SQL literals in already-loaded mnemosyne modules.

    The runtime cursor nets still translate anything missed, but module-level
    rewriting keeps f-string-built statements (e.g. _vec_search_with_blobs)
    from shipping broken shapes past fragile runtime regexes.
    """
    try:
        from . import vector_rewrite
    except Exception as exc:  # pragma: no cover
        _ERRORS.append(f"vec rewrite import: {exc}")
        return
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if name == "mnemosyne" or name.startswith("mnemosyne.") \
           or name == "hermes_memory_provider" or name.startswith("hermes_memory_provider."):
            try:
                vector_rewrite.rewrite_module(mod)
            except Exception as exc:  # pragma: no cover
                _ERRORS.append(f"vec rewrite {name}: {exc}")


# Bind on import.
bind_vendor()
