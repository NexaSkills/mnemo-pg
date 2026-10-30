#!/usr/bin/env python3
"""lcm_store_guard_install.py — LCM sqlite→PG convert-backstop for the
lcm-pg venv, via a site bootstrap that WORKS under uv base pythons.

Hook reality (verified 2026-10-30 on beez):
  * uv-managed bases are built HAVE_SITEcustomize=0 -> usercustomize is
    never executed.
  * multiline .pth lines abort the whole add-package loop (only line 1
    ran; the guard silently died).
  * sitecustomize IS honoured: it lives in the STDLIB dir of the uv base
    python312/; venvs put their own site-packages on sys.path AFTER the
    stdlib dir, so a sitecustomize.py placed in stdlib executes first for
    every venv on that base (and its `import sitecustomize` re-entry from
    the base's own mechanism resolves to the same module — installed
    marker makes it idempotent).

Writes  <uv-base>/lib/python3.12/sitecustomize.py  (marker-guarded,
fail-open). When LCM_BACKEND/MNEMO_BACKEND resolves to postgres it wraps
sqlite3.connect so any connect naming an LCM store (lcm.db, lcm_*.db,
.db under an .../lcm.../ dir) converts to a native PostgreSQL connection
on the lcm-pg cluster — never suppressed, never left as sqlite.
"""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

MARKER = "mnemo-pg-lcm-store-guard"


def _stdlib_dirs():
    seen = []
    for pat in (str(Path.home() / ".local/share/uv/python/*/lib/python3*"),):
        for d in sorted(glob.glob(pat)):
            if os.path.isdir(d) and not re_isolated(d):
                seen.append(Path(d))
    return seen


def re_isolated(d: str) -> bool:
    # skip version-less dirs like .../python312/lib-dynload siblings we
    # do not want; only accept dirs that contain the stdlib os.py
    return not os.path.exists(os.path.join(d, "os.py"))


def main() -> int:
    repo = str(Path.home() / "workspace/mnemo-pg/repo")
    wrote = []
    body = f'''"""sitecustomize — installed by mnemo-pg ({MARKER}).

Chains to any pre-existing sitecustomize via sitecustomize_orig.py (kept
aside on first install). Fail-open: the guard never breaks interpreter
startup.
"""
import os as _os, sys as _sys
_MNEMO_MARKER = {MARKER!r}
_REPO = _os.environ.get("MNEMO_REPO", {repo!r})
try:
    if _REPO not in _sys.path:
        _sys.path.append(_REPO)
    import mnemo_backends.lcm_store_guard as _g
    _g.install()
except Exception:
    pass
'''
    for d in _stdlib_dirs():
        target = d / "sitecustomize.py"
        if target.exists():
            text = target.read_text(errors="ignore")
            if MARKER in text:
                continue
            # chain: rename existing, keep it running after ours
            orig = d / "sitecustomize_orig.py"
            if not orig.exists():
                target.rename(orig)
            target.write_text(body + "\ntry:\n    import sitecustomize_orig\nexcept Exception:\n    pass\n")
        else:
            target.write_text(body)
        wrote.append(str(target))
    for w in wrote:
        print("installed sitecustomize:", w)
    if not wrote:
        print("already installed / no uv stdlib dirs found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
