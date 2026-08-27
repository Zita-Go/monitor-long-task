#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
skill_source="$repository_root/skills/monitor-long-task"

if [[ -n ${CODEX_HOME:-} ]]; then
  codex_root=$CODEX_HOME
elif [[ -n ${HOME:-} ]]; then
  codex_root="$HOME/.codex"
else
  echo "Neither CODEX_HOME nor HOME is set." >&2
  exit 1
fi

skill_destination="$codex_root/skills/monitor-long-task"
if [[ -e $skill_destination ]]; then
  echo "Refusing to overwrite existing skill: $skill_destination" >&2
  exit 2
fi

install -d -m 700 "$codex_root/skills"
cp -R "$skill_source" "$skill_destination"
chmod 755 "$skill_destination" "$skill_destination/agents" "$skill_destination/scripts"
chmod 644 "$skill_destination/SKILL.md" "$skill_destination/agents/openai.yaml"
chmod 755 "$skill_destination/scripts/long_task_monitor.py"

echo "Installed monitor-long-task at $skill_destination"
echo "Configure the webhook with:"
echo "  python3 $skill_destination/scripts/long_task_monitor.py configure"
