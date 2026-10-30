"""Loader-robust legacy-provider neutralizer (single source of truth).

The old sqlite binding installed the `mnemosyne_hermes` package into the
Hermes venv site-packages. Hermes' built-in provider route imports that
package name directly, so its provider singleton would answer agent tool
calls with the OLD sqlite-era code (BEGIN IMMEDIATE, no default=str,
sqlite-vec backfill) alongside the mnemo-pg PG-native provider — a G3
violation (old surface must never serve).

Installs an inert stub into sys.modules under `mnemosyne_hermes` BEFORE
bindings install, so any `import mnemosyne_hermes` resolves to this dead
module and can never register a provider. Idempotent; safe to call twice
and from multiple loader copies.
"""
from __future__ import annotations

import sys
import types


def neutralize() -> None:
    existing = sys.modules.get("mnemosyne_hermes")
    if existing is not None:
        return  # something already owns the name (another loader's stub,
                # or a repo-controlled copy — don't fight it)
    try:
        import importlib.util

        spec = importlib.util.find_spec("mnemosyne_hermes")
    except Exception:
        spec = None
    if spec is None or spec.origin is None:
        return  # not installed here: nothing to neutralize
    if "workspace/mnemo-pg" in spec.origin.replace("\\", "/"):
        return  # already a repo-controlled copy
    stub = types.ModuleType("mnemosyne_hermes")
    stub.__doc__ = (
        "Inert stub installed by mnemo-pg. The legacy pip package is "
        "bypassed: mnemo-pg's vendored provider is the only mnemosyne "
        "surface. See the repo plugin loader + _neutralize_legacy.py."
    )
    stub.__file__ = spec.origin
    stub.__path__ = []  # mark as (empty) package; subimports fail
    setattr(stub, "__version__", "0.0.0-mnemo-pg-stub")

    def _dead(*_a, **_k):
        raise RuntimeError(
            "mnemosyne_hermes is disabled by mnemo-pg; use the mnemosyne_* "
            "tools served by the mnemo-pg provider")

    for _name in ("register", "register_memory_provider",
                  "get_memory_provider", "initialize", "init"):
        setattr(stub, _name, _dead)
    sys.modules["mnemosyne_hermes"] = stub
