#!/usr/bin/env python3
"""gate_before_retire.py — verify PG serves live writes for every profile.

For every profile with a live SQLite bank on disk: write a marker through the
plugin path (mnemo_backends.install + engine), prove it landed in the profile's
PG schema AND that the SQLite file was not touched (mtime unchanged).

Note: banks/<name> sub-bank DBs are deliberately EXCLUDED — they are
consolidation-internal stores owned by the engine (sleep/consolidation), not
per-agent provider banks; the fleet provider path only ever opens the per-home
data/mnemosyne.db (schema mnemosyne_<profile>). Retirement archives the whole
tree, so nothing is lost by not gating those paths.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import os, pathlib, subprocess, sys, time

REPO = pathlib.Path(__file__).resolve().parents[1]
HERMES = pathlib.Path.home() / ".hermes"
SOCK = HERMES / "lcm-pg/run"
if not SOCK.exists():
    SOCK = pathlib.Path.home() / "lcm-pg/run"

def profiles():
    yield "default", HERMES
    for p in sorted((HERMES / "profiles").glob("*/")):
        if (p / "config.yaml").exists():
            yield p.name, p

def main():
    fails = 0
    for name, home in profiles():
        db = home / "mnemosyne/data/mnemosyne.db"
        if not db.exists():
            print(f"{name:<12} SKIP (no sqlite bank)")
            continue
        mark = f"RETREGATE{name.upper()}{int(time.time())}"  # cleanup after gate:
        #   DELETE FROM working_memory WHERE content LIKE 'retirement gate marker RETREGATE%'
        mt0 = db.stat().st_mtime_ns
        code = f"""
import os
os.environ["HERMES_HOME"]={str(home)!r}
os.environ["MNEMO_BACKEND"]="postgres"
import mnemo_backends.install as _i; _i.install()
from mnemosyne.core.memory import Mnemosyne
eng=Mnemosyne(db_path={str(db)!r})
eng.remember("retirement gate marker {mark}", importance=0.5)
print("WROTE")
"""
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": str(REPO)}, timeout=120)
        import psycopg
        schema = "mnemosyne_default" if name == "default" else f"mnemosyne_{name}"
        conn = psycopg.connect(host=str(SOCK), port=54329, dbname="lcm", user="lcm01",
                               options=f"-c search_path={schema},public")
        n = conn.execute("SELECT count(*) FROM working_memory WHERE content LIKE %s",
                         (f"%{mark}%",)).fetchone()[0]
        conn.close()
        mt1 = db.stat().st_mtime_ns
        ok = n == 1 and mt0 == mt1
        if not ok:
            fails += 1
        print(f"{name:<12} {'OK' if ok else 'FAIL'} pg_has_marker={n} "
              f"sqlite_touched={mt0 != mt1} write_rc={r.returncode}")
        if r.returncode != 0:
            print("   stderr tail:", (r.stderr or "").strip().splitlines()[-1:] or "")
    # clean up gate markers from all schemas
    import psycopg
    for name, home in profiles():
        schema = "mnemosyne_default" if name == "default" else f"mnemosyne_{name}"
        try:
            c = psycopg.connect(host=str(SOCK), port=54329, dbname="lcm", user="lcm01",
                                options=f"-c search_path={schema},public")
            d = c.execute("DELETE FROM working_memory WHERE content LIKE %s",
                         ("retirement gate marker RETREGATE%",)).fetchall()
            c.commit(); c.close()
        except Exception:
            pass
    print("GATE:", "PASS" if fails == 0 else f"FAIL ({fails})")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
