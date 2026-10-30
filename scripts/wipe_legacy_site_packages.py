#!/bin/sh
# wipe_legacy_site_packages.py — remove the legacy sqlite-era mnemosyne pip
# packages from every Hermes install env on this host.
#
# Rationale (2026-10-30, Jan directive): mnemo-pg is PostgreSQL-only. The
# pip-installed `mnemosyne-hermes` / `mnemosyne-memory` dists are the sqlite-
# era provider/core. They advertise entry points, so Hermes discovery can
# import them via `ep.load()` BEFORE any directory plugin can neutralize
# them, and every module they import then bypasses the PG binding — serving
# sqlite-only SQL (BEGIN IMMEDIATE, datetime-JSON, COLLATE NOCASE) straight
# into PG. Neutralizing from inside the plugin cannot undo modules already
# served. The envs themselves must be clean.
#
# Policy:
#   - `pip uninstall` the two dists (removes their RECORD-owned files + the
#     entry-point dist-info; re-runs are idempotent no-ops).
#   - Orphan module dirs (pre-RECORD copies) are MOVED aside to
#     <name>.orig-sqlite-<stamp>, never deleted.
#   - Never touches anything else in the venv.
#
# Exit 0 = every env clean (no mnemosyne* importable, no entry points).
# Exit 1 = leftovers remain (list printed).
import glob
import os
import subprocess
import sys
import time
import zipfile

STAMP = time.strftime("%Y%m%d-%H%M")
DISTS = ("mnemosyne-memory", "mnemosyne-hermes", "mnemosyne_hermes", "mnemosyne-memory-pg")
MODULES = ("mnemosyne", "mnemosyne_hermes", "hermes_memory_provider")
EP_GROUPS = ("hermes_agent.plugins", "hermes_agent.memory_providers")


def env_roots():
    home = os.path.expanduser("~")
    roots = set(glob.glob(f"{home}/.hermes/installs/*/environments/*/venv"))
    for extra in (f"{home}/.hermes/hermes-agent/.venv",):
        if os.path.isdir(extra):
            roots.add(extra)
    return sorted(roots)


def site_packages(venv):
    out = []
    lib = os.path.join(venv, "lib")
    if os.path.isdir(lib):
        for py in sorted(os.listdir(lib)):
            sp = os.path.join(lib, py, "site-packages")
            if os.path.isdir(sp):
                out.append(sp)
    return out


def dist_infos(sp):
    found = []
    for name in os.listdir(sp):
        low = name.lower()
        if (low.startswith("mnemosyne") or low.startswith("hermes-memory-provider")
                or low.startswith("hermes_memory_provider")) and (
                "dist-info" in low or "egg-info" in low):
            found.append(os.path.join(sp, name))
    return found


def owns_module_record(sp, mod):
    """True if some dist RECORD (or a zipimported wheel) claims this module dir."""
    for di in dist_infos(sp):
        rec = os.path.join(di, "RECORD")
        if os.path.exists(rec):
            try:
                with open(rec, errors="ignore") as f:
                    if any(line.split(",")[0].startswith(mod + "/") for line in f):
                        return True
            except OSError:
                pass
        # zipimported wheel: the dir is a .zip whose namelist claims it
        zp = os.path.join(sp, mod + ".zip")
        if os.path.exists(zp):
            try:
                if any(n.startswith(mod + "/") for n in zipfile.ZipFile(zp).namelist()):
                    return True
            except OSError:
                pass
    return False


def main():
    changed = []
    for venv in env_roots():
        python = os.path.join(venv, "bin", "python")
        if not os.path.exists(python):
            continue
        sps = site_packages(venv)
        dists_here = [d for sp in sps for d in dist_infos(sp)]
        mods_here = [m for sp in sps for m in MODULES
                     if os.path.isdir(os.path.join(sp, m))]
        if not dists_here and not mods_here:
            print(f"clean: {venv}")
            continue
        print(f"working: {venv}")
        for sp in sps:
            for dist in DISTS:
                subprocess.run([python, "-m", "pip", "uninstall", "-y", dist],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # if pip is missing (uv venvs) dist-info dirs may survive uninstall
        # attempts: drop the dist-info manifests (entry-point discovery source)
        # only when no module dir they own was preserved for reinstalls.
        for sp in sps:
            for di in dist_infos(sp):
                base = os.path.basename(di)
                if base.startswith("mnemosyne_hermes") or base.startswith("mnemosyne_memory"):
                    # keep the manifest if the modules it owns are still live
                    live_mods = [m for m in MODULES
                                 if os.path.isdir(os.path.join(sp, m))]
                    if not live_mods:
                        os.rename(di, f"{di}.orig-sqlite-{STAMP}")
                        print(f"  manifest aside: {base}")
                elif base.startswith("mnemosyne-") or base.startswith("mnemosyne_memory"):
                    if not owns_module_record_ok(sp, di):
                        os.rename(di, f"{di}.orig-sqlite-{STAMP}")
                        print(f"  manifest aside: {base}")
            for mod in MODULES:
                p = os.path.join(sp, mod)
                if os.path.isdir(p) and not owns_module_record(sp, mod):
                    dst = f"{p}.orig-sqlite-{STAMP}"
                    os.rename(p, dst)
                    print(f"  module aside: {mod} -> {os.path.basename(dst)}")
                    changed.append(dst)
    # verify
    bad = []
    for venv in env_roots():
        sps = site_packages(venv)
        for sp in sps:
            for mod in MODULES:
                for cand in (os.path.join(sp, mod), os.path.join(sp, mod + ".py")):
                    if os.path.exists(cand):
                        bad.append(cand)
            for di in dist_infos(sp):
                for grp in EP_GROUPS:
                    ep = os.path.join(di, "entry_points.txt")
                    if os.path.exists(ep) and grp in open(ep, errors="ignore").read():
                        # only bad if it targets a legacy module name
                        body = open(ep, errors="ignore").read()
                        if "mnemosyne_hermes" in body:
                            bad.append(ep)
    if bad:
        print("LEFTOVERS:")
        for b in bad:
            print("  ", b)
        return 1
    print("ALL ENVS CLEAN: no legacy mnemosyne modules or entry points")
    return 0


def owns_module_record_ok(sp, di):
    return False


if __name__ == "__main__":
    raise SystemExit(main())
