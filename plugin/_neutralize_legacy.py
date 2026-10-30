"""Neutralize the legacy pip provider `mnemosyne_hermes`.

The old sqlite binding installed the `mnemosyne_hermes` package into the
Hermes venv site-packages. Hermes plugin discovery imports it directly from
there (it is a top-level package, not shadowed by vendor/), so its provider
singleton would answer agent tool calls with the OLD sqlite-era code paths
(BEGIN IMMEDIATE, sqlite-vec backfill, stale canonical guards) alongside the
mnemo-pg PG-native provider — a G3 violation (old surface must never serve).

This module installs an inert stub into sys.modules under the name
`mnemosyne_hermes` BEFORE the loader bootstraps bindings, so any
`import mnemosyne_hermes` resolves to this dead module and can never register
a provider. Discovery failures are swallowed by the plugin loader (same
behavior as before 0.7.4 existed). Idempotent; safe to import twice.
"""
from __future__ import annotations

import sys
import types


def neutralize() -> None:
    if sys.modules.get("mnemosyne_hermes") is not None:
        return  # something already owns the name (loader stub, or real if
                # a future version of this file decides otherwise — don't fight it)
    try:
        import importlib.util

        spec = importlib.util.find_spec("mnemosyne_hermes")
    except Exception:
        spec = None
    if spec is None or spec.origin is None:
        return  # not installed here: nothing to neutralize
    if "workspace/mnemo-pg" in spec.origin:
        return  # already a repo-controlled copy
    stub = types.ModuleType("mnemosyne_hermes")
    stub.__doc__ = (
        "Inert stub installed by mnemo-pg (mnemosyne plugin loader). The real "
        "legacy pip package is bypassed: mnemo-pg's vendored provider is the "
        "only mnemosyne surface. See plugin/_neutralize_legacy.py."
    )
    stub.__file__ = spec.origin
    stub.__path__ = []           # mark as (empty) package; subimports fail
    setattr(stub, "__version__", "0.0.0-mnemo-pg-stub")

    def _dead(*_a, **_k):
        raise RuntimeError(
            "mnemosyne_hermes is disabled by mnemo-pg; use the mnemosyne_* "
            "tools served by the mnemo-pg provider")

    for _name in ("register", "register_memory_provider", "get_memory_provider",
                  "initialize", "init"):
        setattr(stub, _name, _dead)
    sys.modules["mnemosyne_hermes"] = stub
