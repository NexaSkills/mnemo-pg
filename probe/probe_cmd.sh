#!/usr/bin/env bash
# probe_cmd.sh — command-substitution + heredoc + run_py combo
set -u
R=~/workspace/mnemo-pg/repo
run_py() { PYTHONPATH="$R" MNEMO_PG_TRACE_SQL= "$@" "$R/scripts/run_py.py"; }

d=$(date +%s)
echo "substituted epoch: $d"

python3 - <<'PY'
print("heredoc python OK")
PY

out=$(run_py - <<'PY'
from mnemo_backends import factory
print("backend:", factory.backend_for({}))
import os
os.environ["HERMES_HOME"]="/home/mybuddy/.hermes/profiles/mike"
print("schema:", factory.schema_for_profile())
PY
)
echo "run_py says: $out"
echo done
