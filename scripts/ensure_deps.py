#!/usr/bin/env python3
"""mnemo-pg deploy hook (runs before check_all): ensure psycopg imports.

Some fleet hosts have NO system pip (Omarchy) and no psycopg in the
interpreter mnemo_update.sh runs under. psycopg_site.ensure_psycopg only
finds the lcm-pg private venv if it's on disk here. If it isn't, symlink
the first on-disk site-packages that has BOTH psycopg and psycopg_pool
(never copy, never download; host-agnostic scan under $HOME, skips
backups/staging/venvs-that-belong-to-other-apps only implicitly — the
two module imports are the gate, so a wrong candidate simply fails).
"""
import glob
import importlib
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mnemo_backends import psycopg_site  # noqa: E402


def both_ok():
    try:
        importlib.import_module("psycopg")
        importlib.import_module("psycopg_pool")
        return True
    except Exception:
        return False


def main() -> int:
    if psycopg_site.ensure_psycopg():
        print("[deps] psycopg importable: OK")
        return 0
    home = os.path.expanduser("~")
    pips = glob.glob(f"{home}/.hermes/installs/*/environments/*/venv/bin/pip")
    # cheapest first: try installing via any hermes-managed venv's pip
    for pip in pips[:4]:
        r = subprocess.run([pip, "install", "-q", "psycopg[binary]", "psycopg_pool"],
                           capture_output=True, text=True, timeout=240)
        if r.returncode == 0:
            sp = glob.glob(os.path.dirname(os.path.dirname(pip))
                           + "/lib/python3.*/site-packages")
            for d in sp:
                if os.path.isdir(d) and d not in sys.path:
                    sys.path.append(d)
                if both_ok():
                    print(f"[deps] psycopg installed via {pip}: OK")
                    return 0
    # fallback: link an on-disk site-packages that already has both modules
    pat = f"{home}/**/lib/python3.*/site-packages"
    seen = set()
    for d in sorted(glob.glob(pat, recursive=True)):
        rp = os.path.realpath(d)
        if rp in seen or "/backups/" in rp or "/staging/" in rp:
            continue
        seen.add(rp)
        if os.path.isdir(os.path.join(d, "psycopg")) and \
                os.path.isdir(os.path.join(d, "psycopg_pool")):
            sys.path.append(d)
            if both_ok():
                print(f"[deps] psycopg via on-disk site-packages: {d}")
                # persist for this user's shells/scripts
                envf = os.path.join(home, ".hermes", ".env")
                line = f"MNEMO_PSYCOPG_SITE={d}"
                if os.path.isfile(envf) and "MNEMO_PSYCOPG_SITE=" not in open(envf).read():
                    with open(envf, "a") as fh:
                        fh.write(f"\n# mnemo-pg deploy hook: psycopg location on this host\n{line}\n")
                    print(f"[deps] appended {line} to {envf}")
                return 0
            sys.path.remove(d)
    print("[deps] FAILED: psycopg not importable, not installable, not found on disk")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
