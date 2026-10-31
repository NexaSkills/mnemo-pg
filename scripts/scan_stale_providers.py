#!/usr/bin/env python3
"""scan_stale_providers.py — G1 audit: every dir Hermes' memory-provider loader
could serve as provider 'mnemosyne' must resolve to the mnemo-pg repo plugin.

Hermes' loader (plugins/memory/__init__.py) names loaded provider modules
`_hermes_user_memory.mnemosyne__source_<sha256(realpath)[:16]>`. The
2026-10-31 resurrection wave showed a LEGACY copy serving CLI processes
under a digest that matched NO path on disk = a stale dir since deleted.
This scan enumerates ALL serveable dirs (global plugins, each profile's
plugins, project .hermes/plugins if enabled) and reports any whose digest
differs from the repo plugin's — those are G1 targets (quarantine them).

Exit 0 = every serveable 'mnemosyne' dir IS the repo plugin.
Exit 1 = stale copies found (listed).
"""
from __future__ import annotations
import hashlib, json, os, sys
from pathlib import Path

REPO_PLUGIN = Path.home() / "workspace/mnemo-pg/repo/plugin"
HOME = Path.home() / ".hermes"

def digest(p: Path) -> str:
    return hashlib.sha256(str(p.resolve()).encode()).hexdigest()[:16]

def serveable_dirs() -> list[Path]:
    out = []
    roots = [HOME / "plugins"] + [d / "plugins" for d in sorted((HOME / "profiles").glob("*")) if d.is_dir()]
    if os.environ.get("HERMES_ENABLE_PROJECT_PLUGINS"):
        roots.append(Path.cwd() / ".hermes" / "plugins")
    for r in roots:
        if not r.is_dir():
            continue
        for child in sorted(r.iterdir()):
            if child.name in ("mnemosyne", "mnemosyne_hermes") or child.name.startswith("mnemosyne"):
                init = child / "__init__.py"
                if init.exists():
                    out.append(child)
    return out

def main() -> int:
    want = digest(REPO_PLUGIN)
    stale = []
    for d in serveable_dirs():
        try:
            link = d.resolve()
        except OSError:
            link = d
        g = digest(d)
        status = "ok" if g == want and link == REPO_PLUGIN.resolve() else "STALE"
        print(f"{status:5s} {g}  {d}" + ("" if link == d else f"  -> {link}"))
        if status == "STALE":
            stale.append(str(d))
    if stale:
        print("G1: quarantine the STALE dirs above (they can serve the legacy sqlite provider).")
        return 1
    print("G1 clean: every serveable mnemosyne provider dir is the repo plugin.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
