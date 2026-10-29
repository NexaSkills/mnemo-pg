"""Import audit: prove nothing in the vendored core touches SQLite when the
PG backend is in force.

Strategy: swap sqlite3.connect with a *strict* recorder before importing the
vendored mnemosyne core + provider. Real SQLite is then unreachable — any
call site that still reaches for it raises StrictSQLiteError with the full
Python traceback of the opener, showing exactly which vendored file broke.

Run (from the repo root):
    python3 tests/audit_imports.py             # default: PG backend env required
    MNEMO_BACKEND=sqlite python3 tests/audit_imports.py   # skipped (passes)

Env for the PG path: MNEMO_PG_DSN (same as the plugin), plus optionally
MNEMO_PG_SCHEMA. Nothing here is read-only: importing the provider may
provision schema objects (idempotent DDL) — by design, that IS the test.
"""

import os
import sys
import pathlib
import traceback

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "vendor"))


class StrictSQLiteError(RuntimeError):
    pass


def main() -> int:
    backend = (os.environ.get("MNEMO_BACKEND") or "postgres").strip().lower()
    if backend == "sqlite":
        print("SKIP: sqlite backend (audit only applies to PG mode)")
        return 0
    if not os.environ.get("MNEMO_PG_DSN"):
        print("FAIL: MNEMO_PG_DSN must be set for the PG audit")
        return 1

    import sqlite3 as _s3
    calls = []
    _real_connect = _s3.connect

    def strict_connect(database, *a, **k):
        # :memory: stays real (upstream contract); everything else must go
        # through the backend factory — a raw hit here means a bypass.
        if str(database) == ":memory:":
            return _real_connect(database, *a, **k)
        # factory-backed handles are fine; this hook fires ONLY for calls that
        # bypassed the factory, so record and fail loudly.
        calls.append((str(database), "".join(traceback.format_stack()[-6:-1])))
        raise StrictSQLiteError(
            f"vendored core reached for raw SQLite at: {database}\n"
            + "".join(traceback.format_stack()[-6:-1])
        )

    _s3.connect = strict_connect

    try:
        from mnemo_backends import install as bs_install
        bs_install.install()
        from mnemo_backends import core_swap
    except Exception as e:
        print(f"FAIL: backend install/import: {type(e).__name__}: {e}")
        return 1

    # 1. import the whole vendored core
    import importlib, pkgutil
    import mnemosyne
    mods = ["mnemosyne"]
    for m in pkgutil.walk_packages(mnemosyne.__path__, "mnemosyne."):
        mods.append(m.name)
    mods += ["hermes_memory_provider", "mcp_tools"]
    bad_imports = []
    for name in mods:
        try:
            importlib.import_module(name)
        except Exception as e:
            if isinstance(e, StrictSQLiteError):
                raise
            bad_imports.append((name, f"{type(e).__name__}: {e}"))

    # 2. swap every vendored module's sqlite3 global BEFORE opening anything
    n = core_swap.apply_all()

    # 3. instantiate the engine end-to-end (schema provisioning happens here)
    engine_errors = []
    try:
        os.environ.setdefault("HERMES_HOME", os.environ.get("HERMES_HOME") or str(pathlib.Path.home() / ".hermes"))
        from mnemosyne.core.beam import BeamMemory
        b = BeamMemory(session_id="import-audit")
        b.remember("import audit probe memory", importance=0.1)
        got = b.recall("import audit probe", limit=3)
        b.forget_all(session_id="import-audit") if hasattr(b, "forget_all") else None
    except Exception as e:
        if isinstance(e, StrictSQLiteError):
            raise
        engine_errors.append(f"{type(e).__name__}: {e}")

    # 4. re-scan after runtime imports (lazy modules open at first use)
    n2 = core_swap.apply_all()

    print(f"modules importable: {len(mods) - len(bad_imports)}/{len(mods)}")
    print(f"core modules swapped to PG-aware sqlite3: {n}+{n2}")
    print(f"raw-sqlite bypasses: {len(calls)}")
    for name, err in bad_imports:
        print(f"  import fail {name}: {err}")
    for err in engine_errors:
        print(f"  engine error: {err}")
    ok = not calls and not engine_errors
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


try:
    rc = main()
except StrictSQLiteError as e:
    print("FAIL (strict): vendored core bypassed the backend factory:\n" + str(e))
    rc = 1
sys.exit(rc)
