#!/usr/bin/env bash
# probe_env.sh — where does MNEMO_BACKEND / POLYPHONIC come from?
echo "shell MNEMO_BACKEND=[${MNEMO_BACKEND:-}] POLY=[${MNEMOSYNE_POLYPHONIC_RECALL:-}] HERMES_HOME=[${HERMES_HOME:-}]"
for f in "$HOME/.hermes/.env" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile" /etc/environment; do
  [[ -f "$f" ]] && { m=$(grep -c '^MNEMO_BACKEND=' "$f" 2>/dev/null); p=$(grep -c '^MNEMOSYNE_POLYPHONIC_RECALL=' "$f" 2>/dev/null); echo "$f: backend_lines=$m poly_lines=$p"; }
done
echo "systemd-user:"
systemctl --user show-environment 2>/dev/null | grep -i "MNEMO" | head -5
echo done
