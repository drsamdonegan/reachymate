#!/bin/sh
set -eu
# Run deliberately on the commissioned dedicated kiosk, with admin credentials.
sudo pmset -a sleep 0 disksleep 0 displaysleep 10 standby 0 powernap 0 autorestart 1 womp 1 hibernatemode 0
