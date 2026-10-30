"""Shared-surface PG routing shim (adapter layer, no vendor edits).

Jan directives 2026-10-30:
  * no sqlite databases for mnemosyne or LCM (PostgreSQL only),
  * any call that would open/create one is CONVERTED to native PG, not
    suppressed.

Under the PG backend the shared cross-agent surface reaches disk as a
PATH: _ensure_surface_beam_locked() passes
<provider-root>/data/shared/mnemosyne.db to BeamMemory(db_path=...).
Every real connect of that path is already PG-routed (factory maps paths
containing "shared" to the mnemosyne_shared schema — proven live:
shared_remember rows land as sf_* in mnemosyne_shared.working_memory),
but the surrounding lock machinery (beam._schema_init_lock opens
"<path>.init.lock", BeamMemory mkdir's the parent) materialises an
empty mnemosyne.db-shaped file that looks like a live sqlite store.

Same for init_beam()/init_memory() called with NO db_path: they fall back
to beam._default_db_path() (HOME/.mnemosyne/...mnemosyne.db) purely to
take the flock sidecar.

Fix: rebind those path sources to PG-clean paths —
  * shared surface  -> <HERMES_HOME>/mnemosyne/data/shared-<schema>/shared.db
    (contains "shared" -> factory routes to mnemosyne_shared; name is not
    tripwire-shaped)
  * default path    -> ~/.cache/mnemo-pg/locks/<schema>/store.pg (lock
    tree only; real data connects never used the default anyway)

sqlite_to_pg.py covers every sqlite3.connect door; this module covers the
filesystem-path doors that never go through connect(), AND runs a one-off
startup sweep: any 0-byte mnemosyne.db(-wal/-shm) residue under HOME (from
before the shims existed, or from unbootstrapped processes) is moved into
~/.hermes/backups/sqlite-residue-sweep-<date>/ so no sqlite-shaped artifact
survives; a NON-empty one is a real resurrection and logs loudly instead.
No-op unless the backend is postgres. Idempotent.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

_installed = False
log = logging.getLogger(__name__)


def _shared_pg_path() -> str:
    schema = os.environ.get("MNEMO_SHARED_SCHEMA", "mnemosyne_shared")
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return os.path.join(home, "mnemosyne", "data", f"shared-{schema}", "shared.db")


def _default_lock_path() -> str:
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    schema = os.environ.get("MNEMO_PG_SCHEMA", "default")
    return os.path.join(cache, "mnemo-pg", "locks", schema, "store.pg")


def _sweep_residue() -> None:
    """Move 0-byte mnemosyne.db*/lcm.db* residue under HOME to backups."""
    home = os.path.expanduser("~")
    dest = os.path.join(home, ".hermes", "backups",
                        "sqlite-residue-sweep-" + time.strftime("%Y%m%d"))
    try:
        out = subprocess.run(
            ["find", home, "-maxdepth", "6",
             "(", "-name", "mnemosyne.db", "-o", "-name", "mnemosyne.db-wal",
             "-o", "-name", "mnemosyne.db-shm", "-o", "-name", "lcm.db",
             "-o", "-name", "lcm.db-wal", "-o", "-name", "lcm.db-shm", ")"],
            capture_output=True, text=True, timeout=20).stdout.split()
    except Exception:
        return
    moved = 0
    for f in out:
        if "/backups/" in f or "/staging/" in f or "/state-snapshots/" in f \
                or "/cache/scratch/" in f or "/workspace/" in f:
            continue
        try:
            sz = os.path.getsize(f)
        except OSError:
            continue
        if sz == 0:
            try:
                os.makedirs(dest, exist_ok=True)
                os.rename(f, os.path.join(
                    dest, f.replace(os.sep, "_").lstrip("_")))
                moved += 1
            except OSError:
                pass
        else:
            log.error("mnemo-pg: LIVE sqlite store found outside PG routing: "
                      "%s (%d bytes) — resurrection, investigate", f, sz)
    if moved:
        log.info("mnemo-pg: swept %d 0-byte sqlite residue file(s) to %s",
                 moved, dest)


def install(provider_cls=None) -> bool:
    global _installed
    if _installed:
        return False
    try:
        from . import factory
        if factory.backend_for() != "postgres":
            return False
    except Exception:
        return False

    import pathlib

    # 1. shared surface path on the provider class
    if provider_cls is not None:
        orig = getattr(provider_cls, "_ensure_surface_beam_locked", None)
        if orig is not None and getattr(orig, "__name__", "") != "mnemo_shared_pg":
            def _ensure_surface_beam_locked(self):
                try:
                    cur = str(self._shared_surface_path or "")
                    if "shared-" not in cur:
                        self._shared_surface_path = pathlib.Path(_shared_pg_path())
                except Exception:
                    pass
                return orig(self)
            _ensure_surface_beam_locked.__name__ = "mnemo_shared_pg"
            setattr(provider_cls, "_ensure_surface_beam_locked",
                    _ensure_surface_beam_locked)

    # 2. default path (lock sidecar only) in every mnemosyne.* module that
    # holds a _default_db_path global (beam + memory re-exports).
    for mod in list(sys.modules.values()):
        if getattr(mod, "__name__", "").startswith("mnemosyne") and \
                callable(getattr(mod, "_default_db_path", None)):
            def _default_db_path():
                return pathlib.Path(_default_lock_path())
            _default_db_path.__name__ = "_default_db_path"
            try:
                setattr(mod, "_default_db_path", _default_db_path)
            except Exception:
                pass

    # 2b. bare Mnemosyne(db_path=None) resolves self.db_path from
    # _default_db_path() at call time in some builds and binds the function
    # directly in others; wrap the class property-safe accessor as well.
    try:
        from mnemosyne.core.memory import Mnemosyne as _M
        _orig_init = _M.__init__

        def __init__(self, *a, db_path=None, **kw):
            if db_path is None:
                db_path = _default_lock_path()
            return _orig_init(self, *a, db_path=db_path, **kw)
        __init__.__name__ = "__init__"
        _M.__init__ = __init__
    except Exception:
        pass

    # 3. one-off sweep of pre-shim 0-byte residue
    try:
        _sweep_residue()
    except Exception:
        pass

    _installed = True
    return True
