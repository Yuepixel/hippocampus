#!/usr/bin/env bash
# brainstem-verify.sh - acceptance probe for the brainstem (fourth organ) on 102. ASCII only, read-only.
set -uo pipefail
APP=/opt/hippocampus/brainstem

echo "== [1] service resident =="
echo "brainstem.service active=$(systemctl is-active brainstem.service) enabled=$(systemctl is-enabled brainstem.service 2>&1)"
systemctl show brainstem.service -p MainPID -p NRestarts --no-pager

echo "== [2] liveness: tick counter (must keep growing across restarts) =="
cat "$APP/state/heartbeat.json" 2>/dev/null; echo

echo "== [3] resource account =="
systemctl show brainstem.service -p MemoryCurrent -p CPUUsageNSec --no-pager

echo "== [4] signals tree =="
find "$APP/signals" -type f 2>/dev/null | sort | tail -20 || echo "(none yet)"

echo "== [5] S1/S2/S3/S4 emit evidence (log) =="
grep -E 'emit (s1|s2|s3|s4|s5)' "$APP/logs/brainstem.log" 2>/dev/null | tail -20 || echo "(no emits yet)"
echo "--- probes in last line ---"
tail -3 "$APP/logs/brainstem.log" 2>/dev/null

echo "== [6] S1 consumer wiring =="
echo "brainstem-s1-dream.path active=$(systemctl is-active brainstem-s1-dream.path 2>&1) enabled=$(systemctl is-enabled brainstem-s1-dream.path 2>&1)"
ls -l "$APP/signals/s1/" 2>/dev/null || echo "(no s1 dir yet)"
cat "$APP/logs/consumer-s1.log" 2>/dev/null | tail -5 || echo "(consumer log empty)"

echo "== [7] dream trigger handover state =="
echo "dream.timer enabled=$(systemctl is-enabled dream.timer 2>&1)"
systemctl list-timers dream.timer --no-pager 2>/dev/null | head -2

echo "== [8] boundary self-check (brainstem must not touch the request path / no LLM call) =="
HITS=$(grep -c -E 'curl|v1/chat|:8888' "$APP/brainstem.py" 2>/dev/null || true)
echo "request-path / LLM-call hits: ${HITS:-0} (expect 0)"

echo "== verify done =="
