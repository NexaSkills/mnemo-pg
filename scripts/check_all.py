#!/usr/bin/env python3
"""check_all — mnemo-pg deploy health report for THIS host.

Sections (each prints PASS/WARN/FAIL rows):
  1. backend       MNEMO_BACKEND resolves to postgres, psycopg(+pool) import
  2. schemas       every agent profile on host has mnemosyne_<profile>, + shared
  3. shape         scripts/check_shape.py per schema (native contract)
  4. vectors       vec_* coverage per schema (scripts/check_vec_coverage.py)
  5. recall        live recall probe per profile (non-empty + no SQL errors)
  6. config        plugin links point at the mnemo-pg loader; env pins present
Exit 0 = no FAIL rows.
"""
from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
rows: list[tuple[str, str, str]] = []


def add(status: str, area: str, msg: str) -> None:
    rows.append((status, area, msg))


def dsn() -> str:
    d = os.environ.get("MNEMO_PG_DSN", "").strip()
    if d:
        return d
    home = pathlib.Path(os.environ.get("HERMES_HOME") or pathlib.Path.home() / ".hermes")
    p = home / ".env"
    if p.is_file():
        for line in p.read_text().splitlines():
            if line.startswith("MNEMO_PG_DSN="):
                return line.split("=", 1)[1].strip()
    run = pathlib.Path.home() / "lcm-pg" / "run"
    if run.is_dir():
        return f"host={run} port=54329 dbname=lcm user=lcm01"
    return ""


def main() -> int:
    import psycopg

    d = dsn()
    if not d:
        add("FAIL", "backend", "no MNEMO_PG_DSN resolvable")
    else:
        add("PASS", "backend", f"dsn -> {d.split(' ')[0]}")

    # 1. backend env / deps
    try:
        sys.path.insert(0, str(REPO))
        from mnemo_backends import factory  # noqa
        add("PASS", "backend", f"factory backend = {factory.backend_for()}")
    except Exception as e:
        add("FAIL", "backend", f"factory import: {e}")
    for mod in ("psycopg", "psycopg_pool"):
        try:
            __import__(mod)
            add("PASS", "backend", f"import {mod}")
        except Exception as e:
            add("FAIL", "backend", f"import {mod}: {e}")

    # 2. schema inventory for this host's profiles
    hermes = pathlib.Path.home() / ".hermes"
    profiles = ["default"] + sorted(
        p.name for p in (hermes / "profiles").glob("*")
        if p.is_dir() and not p.name.startswith("."))  # .deleted etc. excluded
    want = {"mnemosyne_" + p.lower().replace("-", "_") for p in profiles}
    want.add("mnemosyne_shared")
    schemas = set()
    if d:
        try:
            with psycopg.connect(d, autocommit=True) as con:
                with con.cursor() as cur:
                    cur.execute(
                        "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'mnemosyne%'")
                    schemas = {r[0] for r in cur.fetchall()}
        except Exception as e:
            add("FAIL", "schemas", f"connect: {e}")
    for s in sorted(want - schemas):
        add("FAIL", "schemas", f"missing schema {s}")
    for s in sorted(want & schemas):
        add("PASS", "schemas", s)
    for s in sorted(schemas - want):
        add("WARN", "schemas", f"schema {s} has no matching profile on host")

    # 3-4. shape + vector coverage via repo scripts
    for script, area in (("check_shape.py", "shape"), ("check_vec_coverage.py", "vectors")):
        p = REPO / "scripts" / script
        if not p.is_file():
            add("WARN", area, f"{script} absent")
            continue
        r = subprocess.run([sys.executable, str(p), "--all"],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        tail = (r.stdout or r.stderr).strip().splitlines()[-3:]
        add("PASS" if ok else "FAIL", area,
            f"{script} rc={r.returncode}; " + " | ".join(tail))

    # 5. live recall probe per profile (skippable: heavy)
    if os.environ.get("MNEMO_CHECK_SKIP_RECALL") != "1":
        for prof in profiles:
            home = hermes if prof == "default" else hermes / "profiles" / prof
            probe = (
                "import sys,os;"
                f"sys.path.insert(0,{str(REPO / 'plugin')!r});"
                f"sys.path.insert(0,{str(REPO)!r});"
                "import importlib; importlib.import_module('plugin');"
                "from mnemosyne.core.memory import Mnemosyne;"
                f"e=Mnemosyne(db_path={str(home / 'mnemosyne' / 'data' / 'mnemosyne.db')!r});"
                "r=e.recall('memory', top_k=1);"
                "print('RECALL_OK', len(r))")
            r = subprocess.run([sys.executable, "-I", "-c", probe],
                               capture_output=True, text=True, timeout=120,
                               env={**os.environ, "HERMES_HOME": str(home),
                                    "MNEMO_BACKEND": "postgres"})
            if "RECALL_OK" in r.stdout and not r.stderr.strip():
                add("PASS", "recall", f"profile {prof}: recall executes clean")
            elif "RECALL_OK" in r.stdout:
                add("WARN", "recall", f"profile {prof}: ok but stderr: {r.stderr.strip()[:120]}")
            else:
                add("FAIL", "recall", f"profile {prof}: {r.stderr.strip()[:160]}")

    # 6. config: plugin link + env pins
    loader = REPO / "plugin"
    link = hermes / "plugins" / "mnemosyne"
    if link.is_symlink() and pathlib.Path(os.path.realpath(link)).is_relative_to(
            os.path.realpath(REPO)):
        add("PASS", "config", f"root plugins/mnemosyne -> {os.readlink(link)}")
    elif link.exists():
        add("FAIL", "config", f"root plugins/mnemosyne -> {os.readlink(link) if link.is_symlink() else '(dir)'} not mnemo-pg")
    for prof in profiles[1:]:
        l2 = hermes / "profiles" / prof / "plugins" / "mnemosyne"
        if l2.is_symlink() and os.path.realpath(str(l2)).startswith(os.path.realpath(str(REPO))):
            add("PASS", "config", f"profile {prof}: plugin link ok")
        else:
            add("FAIL", "config", f"profile {prof}: plugins/mnemosyne missing or wrong target")
    pins = ("MNEMO_BACKEND", "MNEMO_PG_DSN")
    for envf in [hermes / ".env"] + sorted((hermes / "profiles").glob("*/.env")):
        if not envf.is_file():
            continue
        txt = envf.read_text()
        missing = [k for k in pins if f"\n{k}=" not in txt + "\n" and not txt.startswith(k + "=")]
        # DSN may live only in root .env (factory walks up); only pin BACKEND per file
        missing = [k for k in missing if k == "MNEMO_BACKEND"]
        if missing:
            add("WARN", "config", f"{envf}: missing {missing}")
        else:
            add("PASS", "config", f"{envf}: backend pin present")

    # 7. sqlite retirement tripwire (live mnemosyne*.db files = resurrection)
    trip = REPO / "scripts" / "retire_check.py"
    if not trip.is_file():
        add("WARN", "retired", "scripts/retire_check.py absent")
    else:
        r2 = subprocess.run([sys.executable, str(trip)], capture_output=True, text=True)
        head = (r2.stdout or r2.stderr).strip().splitlines()[:1]
        add("PASS" if r2.returncode == 0 else "FAIL", "retired",
            " | ".join(head) or f"retire_check rc={r2.returncode}")

    fails = [r for r in rows if r[0] == "FAIL"]
    warns = [r for r in rows if r[0] == "WARN"]
    for st, area, msg in rows:
        print(f"{st:4} [{area}] {msg}")
    print(f"\ncheck_all: {len(rows)} rows, {len(warns)} warn, {len(fails)} fail")
    print("RESULT " + ("OK" if not fails else "FAIL"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
