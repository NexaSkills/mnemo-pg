"""Adapter-layer fix: make every tool-reply json.dumps datetime-safe.

PG drivers return datetime/date objects where the sqlite store returned ISO
strings, so provider replies containing created_at/timestamp crash plain
json.dumps with "Object of type datetime is not JSON serializable".

Scope discipline: json.dumps is patched ONLY while the vendored provider's
reply-construction code runs (audit hook on MnemosyneMemoryProvider
handle_tool_call / _dispatch_tool_call). Outside those windows json.dumps is
untouched, so no unrelated process code (gateway, other plugins) is affected.
Callers that already pass a `default` (or positional args) are never changed;
the wrapper only kicks in when serialization would otherwise raise TypeError.
"""
from __future__ import annotations

import functools
import json
from contextlib import contextmanager

_MARK = "mnemo_json_safe"
_installed = False


@contextmanager
def _safe_dumps():
    _orig = json.dumps

    def _dumps(obj, *args, **kwargs):
        if args or "default" in kwargs:
            return _orig(obj, *args, **kwargs)
        try:
            return _orig(obj)
        except TypeError:
            return _orig(obj, default=str)

    _dumps.__name__ = _MARK
    json.dumps = _dumps  # type: ignore[assignment]
    try:
        yield
    finally:
        json.dumps = _orig  # type: ignore[assignment]


def install(provider_cls) -> bool:
    """Wrap the provider's tool-reply entrypoints. Idempotent."""
    global _installed
    if _installed or provider_cls is None:
        return False
    for fname in ("handle_tool_call", "_dispatch_tool_call"):
        fn = getattr(provider_cls, fname, None)
        if fn is None or getattr(fn, "__name__", "") == _MARK + "_" + fname:
            continue

        @functools.wraps(fn)
        def _wrapped(*args, _fn=fn, **kwargs):
            with _safe_dumps():
                return _fn(*args, **kwargs)

        _wrapped.__name__ = _MARK + "_" + fname
        setattr(provider_cls, fname, _wrapped)
    _installed = True
    return True
