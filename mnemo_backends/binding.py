"""mnemo_backends.binding — route the vendored core's durable stores to PG.

Native contract: NO SQL translation and NO global stdlib monkeypatch.
`factory.connect(db_path)` decides per path:

  * `:memory:` (and file: URIs naming :memory:) -> real sqlite3
    (the core's transient in-process scratchpad; PG has no equivalent),
  * every durable on-disk bank path             -> NativeConn (PG),
    schema chosen by HERMES_HOME / MNEMO_PG_SCHEMA routing.

Mechanism: each vendored-core (mnemosyne.* / hermes_memory_provider.*)
module gets its module-global name `sqlite3` rebound to a thin proxy
module whose `.connect` is factory.connect. Function bodies like
`sqlite3.connect(path, factory=_BeamConnection)` therefore route per
call, with NO attribute left dangling and NO global patch. Every other
attribute on the proxy resolves to the real stdlib sqlite3 (row factories,
exceptions, Connection) so isinstance/except clauses behave identically.

The stdlib `sqlite3` module itself is NEVER patched: Hermes' own state.db
and every other tool keep real SQLite.

bind() sweeps already-imported modules and installs a meta-path import
hook so lazily-imported submodules are proxied the instant they exec.
"""

from __future__ import annotations

import sqlite3 as _sqlite3
import os
import sys
import types

from . import factory

_BOUND = False
_PREFIXES = ("mnemosyne", "hermes_memory_provider")


class _ProxyModule(types.ModuleType):
    """Module wrapper: `.connect` routes to PG; everything else defers to
    the real stdlib module. Per-module instances so __name__ stays sane."""

    def __init__(self, real: types.ModuleType):
        super().__init__(real.__name__)
        self._real = real
        self.__doc__ = getattr(real, "__doc__", None)

    def __getattr__(self, name):
        if name == "connect":
            return connect
        return getattr(self._real, name)


def _proxy(real: types.ModuleType = _sqlite3) -> _ProxyModule:
    return _ProxyModule(real)


def connect(db_path, *args, **kwargs):
    """Drop-in replacement for sqlite3.connect on the durable path.

    Accepts (and ignores) sqlite3-only kwargs like factory=; the returned
    handle is NativeConn for durable paths, real sqlite3 for :memory:.
    """
    kwargs.pop("factory", None)
    return factory.connect(db_path, *args, **kwargs)


def is_bound() -> bool:
    return _BOUND


def _connect_fn(f):
    """Wrap a module-level `connect = sqlite3.connect` alias so it routes."""

    def _routed(*args, **kwargs):
        kwargs.pop("factory", None)
        return factory.connect(*args, **kwargs)

    _routed.__name__ = "connect"
    _routed.__qualname__ = "binding.routed_connect"
    _routed.__module__ = "mnemo_backends.binding"
    return _routed


def _swap_module(mod) -> None:
    if getattr(mod, "__dict__", None) is None:
        return
    d = mod.__dict__
    try:
        if d.get("sqlite3") is _sqlite3:
            d["sqlite3"] = _proxy(_sqlite3)
        if d.get("connect", None) is _sqlite3.connect:
            # In-place swap (same dict identity) so already-defined
            # functions' LOAD_GLOBAL lookups see the router immediately.
            d["connect"] = _connect_fn(_sqlite3.connect)
    except Exception:
        pass


def _route_dict(d, name: str = "?") -> None:
    """Force any sqlite3 reference in a vendored module dict to route."""
    try:
        cur = d.get("sqlite3")
        if cur is _sqlite3 or type(cur).__name__ not in ("_ProxyModule", "_RoutingModule"):
            d["sqlite3"] = _RoutingModule(name)
        if d.get("connect") is _sqlite3.connect:
            d["connect"] = _connect_fn(_sqlite3.connect)
    except Exception:
        pass


class _RoutingModule(types.ModuleType):
    """sqlite3 stand-in that ALWAYS logs + routes connects."""

    def __init__(self, owner: str):
        super().__init__("sqlite3")
        self._owner = owner

    def __getattr__(self, n):
        if n == "connect":
            owner = self._owner

            def _routed(p, *a, **k):
                if os.environ.get("MNEMO_ROUTE_DEBUG"):
                    print(f"[route] {owner}.sqlite3.connect -> {str(p)[:70]}", file=sys.stderr, flush=True)
                k.pop("factory", None)
                return factory.connect(p, *a, **k)

            return _routed
        return getattr(_sqlite3, n)


class _BindingLoader:
    """Wrap a vendor-core module loader: after exec, proxy its sqlite3."""

    def __init__(self, inner):
        self._inner = inner

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module):
        # During a vendored module's exec, `import sqlite3` must bind the
        # ROUTER (memory.py connects to the DB at module level, during its
        # own exec — post-exec sweeps are too late). Temporarily shadow
        # sys.modules['sqlite3'] for this exec only, then restore.
        _swap_module(module)
        _real_sqlite3_module = sys.modules.get("sqlite3")
        sys.modules["sqlite3"] = _RoutingModule(module.__name__)
        try:
            self._inner.exec_module(module)
        finally:
            if _real_sqlite3_module is not None:
                sys.modules["sqlite3"] = _real_sqlite3_module
        _route_dict(getattr(module, "__dict__", {}), module.__name__)
        _swap_module(module)
        _drop_stale_tls(module)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _drop_stale_tls(mod) -> None:
    """Drop thread-local cached REAL-sqlite handles from a vendored module.

    A module that connected during its own import (before bind()) caches a
    stdlib connection in module-level threading.local state. Real DBs
    cannot be deleted mid-process, so a stale real handle silently serves
    later queries. Closing + dropping it forces reconnection via the proxy.
    """
    try:
        import threading
        for v in list(getattr(mod, "__dict__", {}).values()):
            if isinstance(v, threading.local):
                for name in list(vars(v)):
                    handle = getattr(v, name, None)
                    if isinstance(handle, _sqlite3.Connection):
                        try:
                            handle.close()
                        except Exception:
                            pass
                        try:
                            delattr(v, name)
                        except Exception:
                            pass
    except Exception:
        pass


class _BindingFinder:
    """Meta-path finder: intercepts ONLY the vendored core's submodules."""

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_PREFIXES):
            return None
        if getattr(self, "_reentrant", False):
            return None
        self._reentrant = True
        try:
            return self._find(fullname, path, target)
        finally:
            self._reentrant = False

    def _find(self, fullname, path, target):
        # Wrap ANY spec (importlib.util.find_spec and real imports alike),
        # once per module name — find_spec may be called for probing.
        done = self.__dict__.setdefault("_wrapped", set())
        for finder in sys.meta_path:
            if isinstance(finder, _BindingFinder):
                continue
            try:
                spec = finder.find_spec(fullname, path, target)
            except Exception:
                continue
            if spec is not None and spec.loader is not None:
                if fullname not in done:
                    done.add(fullname)
                    spec.loader = _BindingLoader(spec.loader)
                return spec
        return None


def bind() -> None:
    """Route vendored-core connects to PG (import hook + sweep).

    The finder is installed FIRST (so nothing imports unhooked), then
    already-imported modules are swept. The sweep runs in two waves:
    modules whose bodies import further submodules can define their
    `connect` aliases after the first pass saw them.
    """
    global _BOUND
    if not any(isinstance(f, _BindingFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _BindingFinder())
    for _ in range(2):
        for name, mod in list(sys.modules.items()):
            if name.startswith(_PREFIXES):
                _swap_module(mod)
    _BOUND = True
