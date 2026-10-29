#!/usr/bin/env bash
# mig_parity.sh — per-profile SQLite->PG count parity report.
# Usage: bash mig_parity.sh   (run on the host whose SQLite banks should match PG)
set -u
cat > /tmp/mig_parity.py <<'PY'
import os, pathlib, sqlite3
import psycopg
HOME=pathlib.Path.home()/".hermes"
SOCK=pathlib.Path(os.environ.get("MNEMO_SOCK", HOME/"lcm-pg/run"))
if not SOCK.exists(): SOCK=pathlib.Path.home()/"lcm-pg/run"
TABLES=("working_memory","episodic_memory","facts")
def profiles():
    yield "default", HOME
    for p in sorted((HOME/"profiles").glob("*/")):
        if (p/"config.yaml").exists(): yield p.name, p
bad=0
for name,home in profiles():
    db=home/"mnemosyne/data/mnemosyne.db"
    if not db.exists():
        print(f"{name:<12} SKIP (no sqlite bank)"); continue
    schema="mnemosyne_default" if name=="default" else f"mnemosyne_{name}"
    c=psycopg.connect(host=str(SOCK),port=54329,dbname="lcm",user="lcm01",
                      options=f"-c search_path={schema},public")
    s=sqlite3.connect(f"file:{db}?mode=ro",uri=True)
    row=[]
    for t in TABLES:
        try: ns=s.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        except Exception: ns=-1
        np_=c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        flag="" if ns==np_ else " <-- MISMATCH"; 
        if ns!=np_: bad+=1
        row.append(f"{t} {ns}/{np_}{flag}")
    s.close(); c.close()
    print(f"{name:<12} " + "  ".join(row))
print("PARITY:", "PASS" if bad==0 else f"FAIL ({bad} mismatches)")
PY
python3 /tmp/mig_parity.py
echo done
