#!/usr/bin/env bash
# Live agent remember/recall probe for one profile: writes a unique marker
# through the REAL provider path (mnemosyne_remember tool handler via
# hermes -p <profile> chat one-shot), then verifies the row exists in the
# profile's PG schema. Usage: agent_probe.sh <profile>
set -uo pipefail
p="${1:?profile}"
marker="agentprobe-$(date +%s)-$$"
case "$p" in default) home="$HOME/.hermes";; *) home="$HOME/.hermes/profiles/$p";; esac
timeout 240 hermes -p "$p" chat -q "Call the mnemosyne_remember tool exactly once with content \"$marker\" and importance 0.5, then reply with just the word DONE. Do nothing else." 2>/dev/null | tail -1
sleep 2
found=$(psql -X -h "$HOME/lcm-pg/run" -p 54329 -U lcm01 -d lcm -Atc \
  "select count(*) from mnemosyne_$p.working_memory where content like '%$marker%'" 2>/dev/null)
if [ "${found:-0}" != "0" ]; then echo "AGENT-PROBE $p: PG-HIT ($marker)"; else echo "AGENT-PROBE $p: PG-MISS ($marker)"; fi
