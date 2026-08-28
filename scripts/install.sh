#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/install.sh [--install|--update|--uninstall|--help]

  --install    Install without overwriting an existing skill (default).
  --update     Replace the installed skill and retain the old copy in .backups/.
  --uninstall  Move the installed skill into .disabled/; secrets and task state stay intact.
  --help       Show this help text.
EOF
}

action=install
case "${1:-}" in
  ""|--install|install) action=install ;;
  --update|update) action=update ;;
  --uninstall|uninstall) action=uninstall ;;
  --help|-h|help) usage; exit 0 ;;
  *) usage >&2; exit 64 ;;
esac
if [[ $# -gt 1 ]]; then
  usage >&2
  exit 64
fi

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

skills_root="$codex_root/skills"
skill_destination="$skills_root/monitor-long-task"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)

set_skill_permissions() {
  local target=$1
  chmod 755 "$target" "$target/agents" "$target/scripts"
  chmod 644 "$target/SKILL.md" "$target/agents/openai.yaml"
  chmod 755 "$target/scripts/long_task_monitor.py"
}

stage_skill() {
  install -d -m 700 "$skills_root"
  staging_dir=$(mktemp -d "$skills_root/.monitor-long-task.install.XXXXXX")
  cp -R "$skill_source" "$staging_dir/monitor-long-task"
  set_skill_permissions "$staging_dir/monitor-long-task"
}

case "$action" in
  install)
    if [[ -e $skill_destination || -L $skill_destination ]]; then
      echo "Refusing to overwrite existing skill: $skill_destination" >&2
      echo "Use --update to retain the old copy and install this version." >&2
      exit 2
    fi
    stage_skill
    mv "$staging_dir/monitor-long-task" "$skill_destination"
    rmdir "$staging_dir"
    echo "Installed monitor-long-task at $skill_destination"
    ;;
  update)
    if [[ ! -e $skill_destination && ! -L $skill_destination ]]; then
      echo "Cannot update because the skill is not installed: $skill_destination" >&2
      echo "Run the installer without --update first." >&2
      exit 3
    fi
    stage_skill
    backup_root="$skills_root/.backups"
    backup_destination="$backup_root/monitor-long-task-$timestamp"
    install -d -m 700 "$backup_root"
    if [[ -e $backup_destination || -L $backup_destination ]]; then
      backup_destination="$backup_destination-$$"
    fi
    mv "$skill_destination" "$backup_destination"
    if ! mv "$staging_dir/monitor-long-task" "$skill_destination"; then
      mv "$backup_destination" "$skill_destination"
      echo "Update failed; restored the previous skill." >&2
      exit 1
    fi
    rmdir "$staging_dir"
    echo "Updated monitor-long-task at $skill_destination"
    echo "Previous version retained at $backup_destination"
    ;;
  uninstall)
    if [[ ! -e $skill_destination && ! -L $skill_destination ]]; then
      echo "Skill is not installed: $skill_destination" >&2
      exit 4
    fi
    disabled_root="$skills_root/.disabled"
    disabled_destination="$disabled_root/monitor-long-task-$timestamp"
    install -d -m 700 "$disabled_root"
    if [[ -e $disabled_destination || -L $disabled_destination ]]; then
      disabled_destination="$disabled_destination-$$"
    fi
    mv "$skill_destination" "$disabled_destination"
    echo "Disabled monitor-long-task by moving it to $disabled_destination"
    echo "Webhook secrets and long-task state were not changed."
    exit 0
    ;;
esac

echo "Configure the webhook with:"
echo "  python3 $skill_destination/scripts/long_task_monitor.py configure"
