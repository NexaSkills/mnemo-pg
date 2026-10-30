#!/usr/bin/env bash
# Install planning-with-files (Hermes adapter plugin + skill) from the PUBLIC repo.
# Host-agnostic: resolves OS user home at runtime. Usage: bash probe/pwf_install.sh [profile]
set -euo pipefail
PROFILE="${1:-}"
P=(hermes)
[ -n "$PROFILE" ] && P=(hermes -p "$PROFILE")

"${P[@]}" plugins install OthmanAdi/planning-with-files/.hermes/plugins/planning-with-files 2>&1 | tail -2 || \
  echo "pwf plugin install reported an issue (may already be installed)"
"${P[@]}" plugins enable planning-with-files 2>&1 | tail -1 || true

# Skill: the repo ships skills/planning-with-files; install via hermes skills add (source form may vary)
"${P[@]}" skills add OthmanAdi/planning-with-files 2>&1 | tail -2 || \
  echo "skills add path differed — check repo docs/hermes.md"

echo "--- verify"
"${P[@]}" plugins list 2>/dev/null | grep -i planning || echo "plugin not listed"
ls "${HOME}/.hermes/profiles/${PROFILE:-default}/skills/planning-with-files/SKILL.md" 2>/dev/null \
  || ls "${HOME}/.hermes/skills/planning-with-files/SKILL.md" 2>/dev/null \
  || echo "skill file not found for this profile"
