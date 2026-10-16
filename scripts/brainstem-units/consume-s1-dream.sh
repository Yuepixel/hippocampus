#!/usr/bin/env bash
# consume-s1-dream.sh - brainstem S1 consumer: trigger the dream job, then clear the signal.
# ASCII only. Runs as root (system service, no User=); the dream job runs as user via its unit.
set -uo pipefail
ROOT=/opt/hippocampus/brainstem
SIG="$ROOT/signals/s1/current.json"
LOG="$ROOT/logs/consumer-s1.log"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

if [ ! -f "$SIG" ]; then
  echo "[$(ts)] no S1 signal; nothing to do" >> "$LOG"
  exit 0
fi

echo "[$(ts)] S1 received -> starting dream.service" >> "$LOG"
systemctl start dream.service
rc=$?
echo "[$(ts)] dream.service start rc=$rc" >> "$LOG"

# clear the signal so the path unit re-arms for the next day
rm -f "$SIG"
exit 0
