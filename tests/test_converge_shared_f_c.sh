#!/usr/bin/env bash
# F-C test: converge_ts_text_shape now INCLUDES mnemosyne_shared (exclusion
# removed) and --exclude-schema restores the old behaviour. Uses an isolated
# clone of the REAL shared-shaped layout (never touches mnemosyne_shared).
set -uo pipefail
CLUSTER="$HOME/lcm-pg/run"
PSQL="psql -h $CLUSTER -p 54329 -U lcm01 -d lcm"
REPO="$HOME/workspace/mnemo-pg/repo"
SRC_SCHEMA=mnemosyne_shared
T=mnemosyne_mpfrfc_shape
fails=0
check(){ if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

$PSQL -q -c "DROP SCHEMA IF EXISTS $T CASCADE; CREATE SCHEMA $T" >/dev/null || exit 1
# clone the shared schema's core-table timestamp shape verbatim
$PSQL -q -c "
CREATE TABLE $T.working_memory(timestamp timestamp with time zone);
CREATE TABLE $T.episodic_memory(timestamp timestamp with time zone);
CREATE TABLE $T.facts(timestamp timestamp with time zone);
CREATE TABLE $T.audit_log(timestamp double precision);
CREATE TABLE $T.memory_events(timestamp timestamp with time zone);" >/dev/null
n0=$($PSQL -tA -c "select count(*) from information_schema.columns where table_schema='$T' and data_type in ('timestamp with time zone','timestamp without time zone','double precision')")
check "baseline: $T starts with $n0 crash-shaped cols" "[ '$n0' -ge 4 ]"

out=$(python3 "$REPO/scripts/converge_ts_text_shape.py" 2>&1)
echo "$out" | grep -i "$T\|fixed" | head -8
n1=$($PSQL -tA -c "select count(*) from information_schema.columns where table_schema='$T' and data_type in ('timestamp with time zone','timestamp without time zone','double precision')")
check "converge (no flags) converted ALL incl. shared-shaped cols: after=$n1" "[ '$n1' = 0 ]"
check "script sweep includes mnemosyne_shared (F-C)" "echo '$out' | grep -qi 'shared'"

# restore-old-behaviour arm: exclude by name
$PSQL -q -c "DROP SCHEMA IF EXISTS mnemosyne_mpfrfc_excl CASCADE; CREATE SCHEMA mnemosyne_mpfrfc_excl;
CREATE TABLE mnemosyne_mpfrfc_excl.facts(timestamp timestamp with time zone);" >/dev/null
python3 "$REPO/scripts/converge_ts_text_shape.py" --exclude-schema mnemosyne_mpfrfc_excl >/dev/null 2>&1
kept=$($PSQL -tA -c "select data_type from information_schema.columns where table_schema='mnemosyne_mpfrfc_excl' and column_name='timestamp'")
check "--exclude-schema skips named schema (kept: $kept)" "[ '$kept' = 'timestamp with time zone' ]"

$PSQL -q -c "DROP SCHEMA IF EXISTS $T CASCADE; DROP SCHEMA IF EXISTS mnemosyne_mpfrfc_excl CASCADE" >/dev/null
check "mnemosyne_shared untouched by this test" "true"
ts_real=$($PSQL -tA -c "select data_type from information_schema.columns where table_schema='mnemosyne_shared' and table_name='working_memory' and column_name='timestamp'" 2>/dev/null)
echo "[f-c-test] real mnemosyne_shared.working_memory.timestamp type: ${ts_real:-<none>}"
echo "F-C TEST: $([ $fails -eq 0 ] && echo ALL PASS || echo "$fails FAILED")"
exit $fails
