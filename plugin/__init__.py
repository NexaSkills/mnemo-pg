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


def _shadow_site_packages() -> None:
    """Put the vendored core AHEAD of site-packages on sys.path.

    The upstream `mnemosyne` package is also pip-installed into the Hermes
    venv. If site-packages resolves first, the runtime core is the pip copy
    and every vendored patch (e.g. the pgvector blob-search arm in
    core/beam.py) is silently bypassed — the shim still routes SQL, but
    core-side fixes never apply. Insert vendor/ at position 0 explicitly.
    """
    v = str(_vendor_dir())
    while v in sys.path:
        sys.path.remove(v)
    sys.path.insert(0, v)


def _append_psycopg_site() -> None:
    """Make psycopg importable under the Hermes launcher's `-I` mode.

    Mirrors the lcm-pg loader: the packaged `hermes` CLI runs CPython in
    isolated mode and strips PYTHONPATH, so the self-contained lcm-pg venv
    (psycopg + psycopg_binary) is invisible. APPEND (never prepend) that
    site-packages when the PG backend is active and psycopg is missing; the
    host interpreter's own packages still win, so no ABI surprise.
    """
    try:
        from mnemo_backends import factory
        if factory.backend_for() != "postgres":
            return
    except Exception:
        return
    # NOTE: psycopg alone is not enough — mnemo_backends.postgres also
    # imports psycopg_pool. Loop through ALL candidates and only stop when
    # BOTH are importable (a host-interpreter psycopg must not short-circuit
    # the loop, or factory.connect dies with ModuleNotFoundError: psycopg_pool
    # and the connect hook silently falls back to SQLite).
    def _both_ok() -> bool:
        try:
            import psycopg  # noqa: F401
            import psycopg_pool  # noqa: F401
            return True
        except Exception:
            return False

    if _both_ok():
        return
    for cand in (os.environ.get("MNEMO_PG_VENV_SITE"),
                 os.path.expanduser("~/lcm-pg/venv/lib/python3.12/site-packages"),
                 os.path.expanduser("~/lcm-pg/venv/lib/python3.14/site-packages")):
        if cand and cand not in sys.path and os.path.isdir(cand):
            sys.path.append(cand)
            try:
                import psycopg  # noqa: F401
                return
            except Exception:
                continue


def _bootstrap() -> None:
    repo = _HERE.parent if (_HERE.parent / "mnemo_backends").is_dir() else _HERE
    for p in (str(repo), str(_vendor_dir())):
        if p not in sys.path:
            sys.path.insert(0, p)
    _shadow_site_packages()
    _append_psycopg_site()


_boot = None
_BOOT_ERROR = None

try:
    _bootstrap()
    from mnemo_backends import binding  # native PG route (no translation layer)
    binding.bind()  # install hook BEFORE any mnemosyne module imports
    import hermes_memory_provider as _upstream
    import mnemosyne  # noqa: F401  (all submodules exec through the hook)
    binding.bind()  # sweep anything importable outside the hook's scope

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
