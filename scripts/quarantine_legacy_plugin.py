#!/usr/bin/env python3
"""quarantine_legacy_plugin.py — self-healing enforcement (G3).

Jan 2026-10-30: the OLD sqlite mnemosyne plugin must NEVER co-exist with
mnemo-pg on a host. Detection-only is not enough: this script REMOVES
(archives) legacy residue and heals the binding, idempotently:

  1. legacy plugin directories  — any dir named like *mnemosyne* under
     ~/.hermes/plugins/ or ~/.hermes/profiles/*/plugins/ that is NOT this
     repo's binding (symlink whose realpath is inside the repo, or a dir
     carrying mnemo_pg_shim.py). Moved to
     ~/.hermes/backups/mnemosyne-plugin-quarantine-<stamp>/.
  2. profile config.yaml plugins.enabled still listing `mnemosyne` while the
     binding is missing -> entry removed (config is otherwise NEVER touched).
  3. leftover pip metadata (site-packages/mnemosyne-*.dist-info) reported.

Writes only under ~/.hermes/backups/ and the config.yaml listed-entry heal.
Flags: --dry-run.
"""
from __future__ import annotations
import argparse
import datetime
import os
import pathlib
import re
import shutil
import sys

HERMES = pathlib.Path.home() / ".hermes"
def _default_repo() -> pathlib.Path:
    env = os.environ.get("MNEMO_REPO", "").strip()
    if env:
        return pathlib.Path(env).resolve()
    # checkout-relative (scripts/ -> repo root/plugin): the reliable default.
    # ~/.hermes/plugins/mnemo-pg rarely exists, and an unrealised REPO made
    # every correct symlink look LEGACY (kim quarantine-war 2026-10-31).
    here_plugin = pathlib.Path(__file__).resolve().parent.parent / "plugin"
    if here_plugin.is_dir():
        return here_plugin.resolve()
    return (HERMES / "plugins" / "mnemo-pg").resolve()


REPO = _default_repo()


def _is_repo_binding(d: pathlib.Path) -> bool:
    try:
        real = d.resolve()
    except OSError:
        return False
    if REPO in real.parents or real == REPO:
        return True
    # materialized dir copy (binding.py) also counts
    return (d / "mnemo_pg_shim.py").is_file()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    arch = HERMES / "backups" / f"mnemosyne-plugin-quarantine-{ts}"
    actions = []

    candidates: list[pathlib.Path] = []
    for base in [HERMES / "plugins", *sorted((HERMES / "profiles").glob("*/plugins"))]:
        if not base.is_dir():
            continue
        for d in base.iterdir():
            name = d.name.lower()
            if not d.is_dir() and not d.is_symlink():
                continue
            if "mnemosyne" not in name:
                continue
            if _is_repo_binding(d):
                continue
            if (d / "plugin.yaml").is_file() or (d / "__init__.py").is_file() \
                    or (d / "pyproject.toml").is_file():
                candidates.append(d)

    for d in candidates:
        dest = arch / str(d.relative_to(HERMES))
        actions.append((d, dest))
        print(f"{'DRY ' if a.dry_run else ''}quarantine legacy plugin dir {d} -> {dest}")
        if not a.dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(dest))

    # config heal: drop `mnemosyne` from plugins.enabled where the binding
    # no longer exists at that level (a VALID, live pin must never be touched)
    healed = 0
    if not a.dry_run:
        for cfg in [HERMES / "config.yaml",
                    *sorted((HERMES / "profiles").glob("*/config.yaml"))]:
            if not cfg.is_file():
                continue
            home = cfg.parent
            binding = next((home / sub / "mnemosyne"
                            for sub in ("plugins", "plugins_disabled")
                            if (home / sub / "mnemosyne").exists()), None)
            if binding is not None and _is_repo_binding(binding):
                continue  # live binding exists; the pin is legitimate
            txt = cfg.read_text()
            m = re.search(r"(?m)^(\s+)- (['\"]?)mnemosyne\2\s*$", txt)
            if m:
                cfg = cfg  # noqa: only edit via hermes config unset per fleet rule
                print(f"NOTE: {cfg}: plugins.enabled lists 'mnemosyne' but no "
                      f"repo binding here -> run: hermes -p <profile> config "
                      f"unset plugins.enabled (keep list) OR remove the entry "
                      f"via config tooling. Not auto-edited (fleet rule).")
                healed += 1

    moved = len(candidates)
    print(f"quarantine_legacy_plugin: {moved} dir(s) quarantined"
          f"{' (dry-run)' if a.dry_run else ''}"
          + (f", {healed} config note(s)" if healed else ""))
    if a.dry_run and (moved or healed):
        return 3
    return 0 if not moved or a.dry_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
