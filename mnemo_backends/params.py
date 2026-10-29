"""Paramstyle rendering: sqlite3 placeholders -> psycopg3 composed SQL.

No DATA is ever inlined: this composes PLACEHOLDER references only.
Values always travel as driver parameters (positional or named), except
two shapes psycopg3 cannot express as parameters:

  * repeated-index placeholders (`?1` used twice) — the same bound
    value is referenced twice; we emit it as a hand-escaped SQL
    *literal* (the sole place SQL text embeds a value; escaped via
    psycopg's own quoting machinery, never str-concat),
  * bare `%` (sqlite3 treats a lone percent literally).

Mapping:
  ?       -> %s / %(pN)s   (consumes the next arg in order)
  ?N      -> literal of args[N]      (repeat-capable form)
  :name   -> %(name)s                (dict params)
  %s      -> %s                      (already-PG marker)
  %(k)s   -> %(k)s                   (already-PG named marker)
  %%      -> %                       (sqlite3 driver-escape parity)
  %<c>    -> treated as %s (conversion specifiers other than s are
            not emitted by the vendored core)

Placeholders inside string literals, quoted identifiers, and comments
are left untouched (tokenised scan, not blind regex).

render() returns (native_sql, params_out) where params_out is None
(marker-free — caller executes via psycopg.sql.SQL verbatim), a tuple
(positional), or a dict (named). Binding errors mirror
sqlite3.ProgrammingError.
"""

from __future__ import annotations

import re
import sqlite3 as _sqlite3

import psycopg
import psycopg.adapt


_TOKEN = re.compile(
    r"""
      (?P<str>'(?:[^']|'')*')
    | (?P<dq>"(?:[^"]|"")*")
    | (?P<line>--[^\n]*)
    | (?P<block>/\*.*?\*/)
    | (?P<namedmod>%\([A-Za-z_]\w*\)s)
    | (?P<mod>%%|%s|%[a-zA-Z])
    | (?P<pos>\?\d+)
    | (?P<q>\?)
    | (?P<named>(?<!:):[A-Za-z_]\w*)
    """,
    re.VERBOSE | re.DOTALL,
)


class _Lumper(psycopg.adapt.Dumper):
    """Dumps a scalar as a literal for reuse in ?N-repeat positions."""


def _literal(conn, value) -> str:
    """A safe SQL literal for `value`. Only used for repeated-index
    placeholders (?N), which psycopg cannot express as parameters.
    Every branch produces a fully-quoted form — never raw str()."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return f"'{value}'::double precision"
        return repr(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "decode('" + bytes(value).hex() + "', 'hex')::bytea"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'::text"
    # datetime/date/Decimal/uuid etc: render via psycopg's text dumper,
    # then assert the result is a safe literal; else quote as text.
    try:
        dumper = conn.adapters.get_dumper(type(value), psycopg.pq.Format.TEXT)
        s = dumper.dump(value)
        s = s.decode() if isinstance(s, bytes) else s
        if not re.fullmatch(r"(?:'(?:[^']|'')*'|NULL|[-0-9.eE+]+)", str(s)):
            raise TypeError("unquoted dump form")
        return str(s)
    except Exception:
        return "'" + str(value).replace("'", "''") + "'::text"


def render(sql: str, params=None, conn=None):
    args = params if params is not None else ()
    named_in = args if isinstance(args, dict) else None
    seq = () if named_in is not None else tuple(args)

    parts: list[str] = []
    positional: list = []          # used only while style == 'pos'
    named: dict = {}
    style: list = [None]           # None | 'pos' | 'named'
    consumed = [0]

    def consume(idx):
        if named_in is not None:
            raise _sqlite3.ProgrammingError(
                "cannot mix ? and named placeholders")
        if idx is None:
            if consumed[0] >= len(seq):
                raise _sqlite3.ProgrammingError(
                    "tuple index out of range")
            v = seq[consumed[0]]
            consumed[0] += 1
            return v
        if idx >= len(seq):
            raise _sqlite3.ProgrammingError(
                f"index {idx} out of range for {len(seq)} parameters")
        return seq[idx]

    def to_named():
        """Convert emitted positional markers to named style in place."""
        new_parts = []
        for p in parts:
            new_parts.append(p)
        # map the k-th "%s" entry to pK
        k = 0
        fixed = []
        for p in new_parts:
            if p == "%s":
                fixed.append(f"%(p{k})s")
                named[f"p{k}"] = positional[k]
                k += 1
            else:
                fixed.append(p)
        assert k == len(positional)
        parts[:] = fixed
        positional.clear()
        style[0] = "named"

    def emit(value, prefer_named=False):
        if style[0] is None:
            style[0] = "named" if prefer_named else "pos"
        if style[0] == "pos":
            if prefer_named:
                to_named()
            else:
                positional.append(value)
                return "%s"
        key = f"n{len(named)}"
        named[key] = value
        return f"%({key})s"

    last = 0
    for m in _TOKEN.finditer(sql):
        g = m.lastgroup
        parts.append(sql[last:m.start()])   # literal text between tokens
        last = m.end()
        if g == "q":
            parts.append(emit(consume(None)))
            continue
        if g == "pos":
            parts.append(_literal(conn, consume(int(m.group()[1:]))))
            continue
        if g in ("named", "namedmod"):
            name = m.group()[1:] if g == "named" else m.group()[2:-2]
            if named_in is None or name not in named_in:
                raise _sqlite3.ProgrammingError(
                    "a numbered tuple was provided and the clause could "
                    f"not be bound: no parameter for placeholder {name!r}")
            if style[0] == "pos":
                to_named()
            elif style[0] is None:
                style[0] = "named"
            named[name] = named_in[name]
            parts.append(f"%({name})s")
            continue
        if g == "mod":
            tok = m.group()
            if tok == "%%":
                parts.append("%")
                continue
            parts.append(emit(consume(None)))
            continue
        parts.append(m.group())

    parts.append(sql[last:])
    native = "".join(parts)
    if style[0] == "pos":
        return native, tuple(positional)
    if style[0] == "named":
        return native, named
    return native, None


def check_binding(sql: str, params) -> None:
    """sqlite3 parity: raise ProgrammingError when the parameter count
    does not match the marker count (only checked for ?-style)."""
    n = 0
    for m in _TOKEN.finditer(sql):
        if m.lastgroup in ("q",):
            n += 1
    if n == 0:
        return
    if params is None:
        raise _sqlite3.ProgrammingError(
            "Incorrect number of bindings supplied. The statement uses "
            f"{n} and 0 were provided.")
    k = len(params)
    if k != n:
        raise _sqlite3.ProgrammingError(
            "Incorrect number of bindings supplied. The statement uses "
            f"{n} and {k} were provided.")
