"""mnemo-pg loader — native PostgreSQL storage for the mnemosyne provider.

Drop-in replacement for the ``mnemosyne`` plugin directory (the upstream
provider that talks to SQLite). This loader:

1. puts the vendored mnemosyne core + provider on ``sys.path`` (the repo's
   ``vendor/`` dir, or ``$MNEMO_VENDOR_DIR`` when set),
2. binds every ``sqlite3.connect`` in the vendored code through the
   translation backend (``mnemo_backends``; PG by default — see
   ``mnemo_backends/vendor_bind.py``),
3. exposes the upstream provider class so Hermes' plugin machinery drives
   the usual automatic hooks (session start / turn / sync / prefetch /
   shutdown) exactly as before.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _vendor_dir() -> Path:
    env = os.environ.get("MNEMO_VENDOR_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    # Layout A: plugin dir is <repo>/plugin (vendor is a sibling).
    cand = _HERE.parent / "vendor"
    if cand.is_dir():
        return cand
    # Layout B: vendor/ was copied INTO the plugin dir (install_plugin.py).
    cand = _HERE / "vendor"
    if cand.is_dir():
        return cand
    raise FileNotFoundError("mnemo-pg: no vendor/ dir found near the plugin")


def _bootstrap() -> None:
    repo = _HERE.parent if (_HERE.parent / "mnemo_backends").is_dir() else _HERE
    for p in (str(repo), str(_vendor_dir())):
        if p not in sys.path:
            sys.path.insert(0, p)


_boot = None
_BOOT_ERROR = None

try:
    _bootstrap()
    from mnemo_backends import vendor_bind  # noqa: F401  (binds on import)
    vendor_bind.bind_vendor()  # ensure stdlib hook is live
    import hermes_memory_provider as _upstream
    import mnemosyne  # noqa: F401  (force core import for per-module swap)
    vendor_bind.bind_vendor()  # second pass over the now-loaded core modules

    _boot = _upstream
except Exception as exc:  # pragma: no cover
    _BOOT_ERROR = f"{type(exc).__name__}: {exc}"


def _delegate(name):
    def _fn(*args, **kwargs):
        if _boot is None:
            raise RuntimeError(f"mnemo-pg failed to load: {_BOOT_ERROR}")
        return getattr(_boot, name)(*args, **kwargs)

    return _fn


# The plugin machinery introspects register() for hook tool schemas, so it
# must be the real upstream function (bound to the loaded module).
if _boot is not None:
    for _fn_name in ("register", "shutdown", "inject_memory_prefix",
                     "initialize"):
        _fn = getattr(_boot, _fn_name, None)
        if _fn is not None:
            globals()[_fn_name] = _fn

    MemoryProvider = _boot.MemoryProvider
    MnemosyneMemoryProvider = _boot.MnemosyneMemoryProvider

    # Pass through everything else the provider module exposes.
    _missing = []
    for _n in dir(_boot):
        if _n.startswith("__"):
            continue
        if _n not in globals():
            globals()[_n] = getattr(_boot, _n)
else:  # pragma: no cover
    def _dead(*a, **k):
        raise RuntimeError(f"mnemo-pg failed to load: {_BOOT_ERROR}")

    register = initialize = shutdown = inject_memory_prefix = _dead
    MemoryProvider = MnemosyneMemoryProvider = None
