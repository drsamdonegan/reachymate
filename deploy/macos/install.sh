#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
task_domain="gui/$(id -u)"
task_agents="$HOME/Library/LaunchAgents"
task_launcher="$task_root/deploy/macos/build/Reachy Concierge.app"
codesign --verify --deep --strict "$task_launcher"
test -x "$task_root/.venv-daemon/bin/reachy-mini-daemon"
test -x "$task_root/.venv/bin/python"
mkdir -p "$task_agents"
"$task_root/.venv/bin/python" "$task_root/deploy/macos/generate_agents.py" --root "$task_root" --output "$task_agents"
for task_name in daemon bouncer watchdog caffeinate; do
  task_file="$task_agents/au.mlai.reachy.$task_name.plist"
  plutil -lint "$task_file"
  launchctl bootstrap "$task_domain" "$task_file"
done
