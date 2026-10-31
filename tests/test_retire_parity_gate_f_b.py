#!/usr/bin/env python3
"""F-B negative+positive test: retire_sqlite parity gate on an isolated
lcm-pg schema + throwaway sqlite db in /tmp. Nothing fleet, nothing real."""
import os, pathlib, shutil, subprocess, sqlite3, sys

REPO = pathlib.Path.home() / "workspace/mnemo-pg/repo"
SCHEMA = "mnemosyne_mpfrtest"
PGD = f"host={pathlib.Path.home()}/lcm-pg/run port=54329 dbname=lcm user=lcm01"
HOMES = pathlib.Path("/tmp/mpfr-fb/homes")
DBDIR = HOMES / "profiles" / "mpfrtest"

import psycopg

def pg(sql, args=None):
    with psycopg.connect(PGD, autocommit=True) as c:
        r = c.execute(sql, args)
        try:
            return r.fetchall()
        except Exception:
            return []

def setup_sqlite():
    if HOMES.exists():
        shutil.rmtree(HOMES)
    DBDIR.mkdir(parents=True)
    con = sqlite3.connect(DBDIR / "mnemosyne.db")
    con.executescript("""
      CREATE TABLE episodes(id INTEGER PRIMARY KEY, ts TEXT, body TEXT);
      INSERT INTO episodes VALUES (1,'2026-01-01T00:00:00','one'),(2,'2026-01-02T00:00:00','two'),(3,'2026-01-03T00:00:00','three');
      CREATE TABLE facts(id INTEGER PRIMARY KEY, val TEXT);
      INSERT INTO facts VALUES (10,'x'),(11,'y');
    """)
    con.commit(); con.close()

def run_retire(extra_args=()):
    env = dict(os.environ)
    env["MNEMO_PG_DSN"] = PGD
    env["MNE_NO_SYSTEMD"] = "1"
    env["MNEMO_RETIRE_HOMES"] = str(HOMES)
    env["MNEMO_TRIPWIRE_UNITS"] = "/tmp/mpfr-fb/units"
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "retire_sqlite.py"), *extra_args],
        capture_output=True, text=True, env=env)

fails = 0

def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("  " + detail if detail and not cond else ""))
    if not cond:
        fails += 1

# --- setup: fresh PG schema + sqlite db, PG missing episode id=3
pg(f'DROP SCHEMA IF EXISTS {SCHEMA} CASCADE')
pg(f'CREATE SCHEMA {SCHEMA}')
with psycopg.connect(PGD, autocommit=True) as c:
    c.execute(f'CREATE TABLE {SCHEMA}.episodes(id bigint primary key, ts text, body text)')
    c.execute(f'CREATE TABLE {SCHEMA}.facts(id bigint primary key, val text)')
    c.execute(f'INSERT INTO {SCHEMA}.episodes VALUES (1,%s,%s),(2,%s,%s)',
              ('2026-01-01T00:00:00','one','2026-01-02T00:00:00','two'))
    c.execute(f'INSERT INTO {SCHEMA}.facts VALUES (10,%s),(11,%s)', ('x','y'))
setup_sqlite()
src = DBDIR / "mnemosyne.db"

print("=== TEST 1 (negative): row 3 missing from PG -> must REFUSE, keep files ===")
r = run_retire()
print(r.stdout[-1500:]); print(r.stderr[-400:])
check("T1 refuses (rc=1, GATE FAIL, source intact)",
      r.returncode == 1 and "GATE: FAIL" in r.stdout and src.exists(),
      f"rc={r.returncode} src={src.exists()}")
check("T1 archived NOTHING", not any(pathlib.Path.home().glob('.hermes/backups/mnemosyne-sqlite-retired-*')))

print("=== TEST 2 (--force bypass works but logs LOUD) ===")
r = run_retire(["--force"])
print(r.stdout[-1200:])
check("T2 force: gate skipped loudly, rc=0",
      r.returncode == 0 and "parity gate SKIPPED" in r.stdout)
# restore db for next tests (force run archived+deleted it)
if src.exists():
    pass
else:
    setup_sqlite()
    with psycopg.connect(PGD, autocommit=True) as c:
        c.execute(f'INSERT INTO {SCHEMA}.episodes VALUES (3,%s,%s)', ('2026-01-03T00:00:00','three'))

print("=== TEST 3 (positive): full parity -> gate PASS ===")
r = run_retire()
print(r.stdout[-1500:]); print(r.stderr[-400:])
check("T3 gate PASS rc=0", r.returncode == 0 and "GATE: PASS" in r.stdout,
      f"rc={r.returncode}")

print("=== TEST 4 (table absent in PG with rows) -> must REFUSE ===")
setup_sqlite()
with psycopg.connect(PGD, autocommit=True) as c:
    c.execute(f'DROP TABLE {SCHEMA}.facts')
r = run_retire()
print(r.stdout[-1200:])
check("T4 refuses, source intact", r.returncode == 1 and src.exists(), f"rc={r.returncode}")

pg(f'DROP SCHEMA IF EXISTS {SCHEMA} CASCADE')
shutil.rmtree("/tmp/mpfr-fb", ignore_errors=True)
print("cleanup done")
sys.exit(fails)
