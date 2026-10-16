#!/usr/bin/env bash
# brainstem-dream-handover.sh - hand the dream trigger over to the brainstem S1 signal (or take it back).
# Usage: bash brainstem-dream-handover.sh on|off    ASCII only.
set -uo pipefail
MODE="${1:-}"
if [ "$MODE" != "on" ] && [ "$MODE" != "off" ]; then
  echo "usage: $0 on|off"; exit 2
fi

if [ "$MODE" = "on" ]; then
  echo "== handover ON: brainstem S1 becomes the dream trigger =="
  sudo -n systemctl disable --now dream.timer
  sudo -n systemctl enable --now brainstem-s1-dream.path
else
  echo "== handover OFF: restore dream.timer as the trigger =="
  sudo -n systemctl disable --now brainstem-s1-dream.path
  sudo -n systemctl enable --now dream.timer
fi

echo "dream.timer            enabled=$(systemctl is-enabled dream.timer 2>&1) active=$(systemctl is-active dream.timer 2>&1)"
echo "brainstem-s1-dream.path enabled=$(systemctl is-enabled brainstem-s1-dream.path 2>&1) active=$(systemctl is-active brainstem-s1-dream.path 2>&1)"
echo "brainstem.service       active=$(systemctl is-active brainstem.service 2>&1)"
echo "== handover $MODE done =="
