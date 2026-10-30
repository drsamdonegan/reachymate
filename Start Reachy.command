#!/bin/zsh
# Finder opens .command files in Terminal, preserving its camera/microphone permission.
cd -- "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print 'Reachy needs its local Python environment. Follow README.md Setup, then reopen this launcher.'
  read -r '?Press Return to close. '
  exit 1
fi
.venv/bin/python -m app.scripts.launch_reachy "$@"
result=$?
if (( result != 0 )) && [[ -t 0 ]]; then
  read -r '?Press Return to close. '
fi
exit "$result"
