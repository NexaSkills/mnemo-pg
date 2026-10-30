"""Global sqlite3.connect → PostgreSQL conversion (Jan directives 2026-10-30).

Rules:
  1. ANY mnemosyne/lcm call that would create or open a sqlite database is
     CONVERTED to native PostgreSQL — never suppressed, never allowed to
     fall back to sqlite.
  2. Hermes' OWN sqlite stores (state.db, kanban.db, cron dbs, ...) are
     untouched — the mandate names lcm and mnemosyne stores only.

How the two halves coexist (both installed only when MNEMO_BACKEND=postgres):

  * binding.py (unchanged, primary): per-module proxies in every vendored
    mnemosyne.*/hermes_memory_provider.* module route their durable
    connects to factory.connect -> NativeConn(PG). Already proven live.
  * this module (backstop for connects issued OUTSIDE those modules —
    legacy fallback handlers, probes, lazily-imported extras): wraps
    stdlib sqlite3.connect with a NAME-SCOPED router:
      - path names a mnemosyne/lcm store -> factory.connect(path) = PG
        conn, schema chosen by the SAME routing as the vendored modules
        (schema_for_profile / MNEMO_PG_SCHEMA / 'shared' -> shared).
        PG failure raises loudly; no silent sqlite fallback.
      - :memory: scratchpads -> pass-through (transient, nothing durable).
      - store paths under a backups/ tree -> reopened read-only (URI
        mode=ro) forensic sqlite; create/rw opens are refused.
      - everything else (Hermes state.db, kanban, cron, unrelated plugins)
        -> stdlib sqlite3, untouched.

File-creation note: stdlib sqlite3.connect materialises an empty .db file
at open. For store paths we let it be created and schedule removal on the
NEXT call (deferred GC) rather than unlinking inside our own open() —
POSIX lets an open fd outlive an unlink, but racing unlink-while-open
against a real (routed-failed) sqlite handle risks data loss; the GC
still removes the artifact within microseconds under our control.

Idempotent, and never breaks boot: any failure leaves sqlite3.connect
exactly as it was.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3

_installed = False
_pending_gc: list[str] = []


def _is_memory(s: str) -> bool:
    return s == ":memory:" or (s.startswith("file:") and ":memory:" in s)


def _is_store_path(s: str) -> bool:
    base = os.path.basename(s.split("?", 1)[0]).lower()
    # mnemosyne stores: any .db whose NAME contains 'mnemosyne' or starts
    # with 'mnemo' (covers mnemosyne.db, mnemosyne_shared.db, mnemo.db).
    if base.endswith(".db") and ("mnemosyne" in base
                                 or base.startswith("mnemo")):
        return True
    # LCM stores by name: lcm.db, lcm_<profile>.db
    if base.startswith("lcm") and base.endswith(".db"):
        return True
    # LCM stores by dir: any .db under an .../lcm.../ directory
    if base.endswith(".db") and "/lcm" in s.replace("\\", "/").lower():
        return True
    return False


def _plain(s: str) -> str:
    return s[5:].split("?", 1)[0] if s.startswith("file:") else s


def _run_gc() -> None:
    while _pending_gc:
        p = _pending_gc.pop()
        try:
            os.unlink(p)
        except OSError:
            pass


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

    _orig_connect = sqlite3.connect

    def _convert_connect(path=":memory:", *args, **kwargs):
        _run_gc()  # remove artifacts from previous converts (deferred GC)
        s = str(path)
        if _is_memory(s):
            return _orig_connect(path, *args, **kwargs)
        if _is_store_path(_plain(s)):
            if "/backups/" in s.replace("\\", "/"):
                # forensic tree: read-only sqlite allowed, writes refused
                if s.startswith("file:"):
                    if "mode=" in s:
                        raise RuntimeError(
                            "mnemo-pg: backups tree is forensic read-only "
                            "sqlite (mode= URI refused): " + s)
                    sep = "&" if "?" in s else "?"
                    return _orig_connect(s + sep + "mode=ro", uri=True,
                                         *args, **kwargs)
                ro = "file:" + pathlib.Path(s).resolve().as_posix() + "?mode=ro"
                return _orig_connect(ro, uri=True)
            try:
                return factory.connect(s, *args, **kwargs)
            finally:
                try:
                    p = pathlib.Path(_plain(s)).expanduser()
                    if p.exists() and p.stat().st_size == 0:
                        _pending_gc.append(str(p))
                except OSError:
                    pass
        return _orig_connect(path, *args, **kwargs)

    _convert_connect.__name__ = "connect"
    try:
        sqlite3.connect = _convert_connect  # type: ignore[attr-defined]
        _installed = True
        return True
    except Exception:
        return False
