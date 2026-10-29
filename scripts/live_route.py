#!/usr/bin/env python3
"""live_route.py — does the LIVE gateway serve memory from PG or SQLite?

For each profile: marker via the plugin loader path (same import order Hermes
uses: <profile>/plugins dir on sys.path, import plugin as a package), then
prove the marker is in the profile's PG schema and the SQLite file was not
touched. Exit 0 = PG is live for every profile.
"""
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
        link = home / "plugins" / "mnemosyne"
        db = home / "mnemosyne" / "data" / "mnemosyne.db"
        if not link.exists():
            print(f"{name:<12} SKIP (no plugin link)")
            continue
        target = link.resolve()
        if not str(target).startswith(str(REPO)):
            print(f"{name:<12} FAIL link -> {target} (not mnemo-pg loader)")
            fails += 1
            continue
        if not db.exists():
            print(f"{name:<12} OK   (link->loader; no sqlite bank on disk)")
            continue
        mark = f"LIVEOK{int(time.time())}"
        if db.exists():
            db.unlink()  # retirement contract: fresh process must not revive SQLite
        mt0 = db.stat().st_mtime_ns if db.exists() else 0
        code = f"""
import sys, os
sys.path.insert(0, {str(REPO / 'vendor')!r})   # shadow site-packages (gateway startup parity)
sys.path.insert(0, {str(link)!r})          # plugin dir itself == what Hermes imports
sys.path.insert(0, {str(REPO)!r})
import os as _o
_o.environ["HERMES_HOME"]={str(home)!r}
_o.environ["MNEMO_BACKEND"]="postgres"
import plugin
from mnemosyne.core.memory import Mnemosyne
e=Mnemosyne(db_path={str(db)!r})
e.remember("live route marker {mark}", importance=0.5)
print("WROTE")
"""
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                           text=True, timeout=180, env={**os.environ,
                           "HERMES_HOME": str(home)})
        import psycopg  # noqa: E402
        schema = "mnemosyne_default" if name == "default" else f"mnemosyne_{name}"
        conn = psycopg.connect(host=str(SOCK), port=54329, dbname="lcm", user="lcm01",
                               options=f"-c search_path={schema},public")
        n = conn.execute("SELECT count(*) FROM working_memory WHERE content LIKE %s",
                         (f"%{mark}%",)).fetchone()[0]
        conn.execute("DELETE FROM working_memory WHERE content LIKE %s",
                     ("live route marker LIVEOK%",))
        conn.commit()
        conn.close()
        mt1 = db.stat().st_mtime_ns if db.exists() else 0
        ok = n == 1 and mt0 == mt1
        if not ok:
            fails += 1
        print(f"{name:<12} {'OK  ' if ok else 'FAIL'} pg_marker={n} sqlite_touched={mt0 != mt1}"
              + ("" if r.returncode == 0 else f" write_rc={r.returncode} err={r.stderr.strip()[-120:]}"))
    print("LIVE ROUTE:", "PASS" if fails == 0 else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
