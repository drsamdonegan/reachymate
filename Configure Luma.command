#!/bin/zsh
# Enter the Luma key in a local Terminal prompt, never in chat or command arguments.
cd -- "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print 'Reachy needs its local Python environment. Follow README.md Setup first.'
  read -r '?Press Return to close. '
  exit 1
fi
.venv/bin/python -m app.scripts.configure_luma "$@"
result=$?
if [[ -t 0 ]]; then
  read -r '?Press Return to close. '
fi
exit "$result"
