#!/bin/bash
# One batched probe: for each profile, recall 3x in a single process
# (pool-resilience: a closed pool fails immediately, healthy pool keeps working).
set -u
VENV=/home/mybuddy/.hermes/installs/9527c3f31db6a735/environments/8c570d1568234fd48f0d2908e8487d5a/venv/bin/python
PY=$VENV
[ -x "$PY" ] || PY=python3
REPO=/home/mybuddy/workspace/mnemo-pg/repo
for p in "$@"; do
  echo "== $p"
  MNEMO_PROFILE="$p" HERMES_HOME=/home/mybuddy/.hermes/profiles/$p \
    timeout 120 "$PY" -I - <<'EOF' 2>&1 | tail -12
import os, sys
repo = "/home/mybuddy/workspace/mnemo-pg/repo"
sys.path.insert(0, repo)
import plugin  # loader: binds connect + shadows vendored core
from mnemosyne import Mnemosyne
eng = Mnemosyne()
routes = []
for i in range(3):
    try:
        hits = eng.recall("zebra", top_k=3)
        routes.append(f"call{i}: OK {len(hits)} hits")
    except Exception as e:
        routes.append(f"call{i}: FAIL {type(e).__name__}: {e}")
# route proof
try:
    import psycopg
    dsn = "host=/home/mybuddy/lcm-pg/run port=54329 dbname=lcm user=lcm01"
    with psycopg.connect(dsn) as conn, conn.cursor() as c:
        c.execute("SET search_path TO mnemosyne_%s, public" % os.environ['MNEMO_PROFILE'])
        c.execute("SELECT count(*) FROM working_memory")
        routes.append(f"pg_wm_rows: {c.fetchone()[0]}")
except Exception as e:
    routes.append(f"pg_proof: FAIL {e}")
print("\n".join(routes))
EOF
done
