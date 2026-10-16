#!/usr/bin/env bash
# dream-4c1-verify.sh - 4c-1 acceptance probe on 102 (read-only except running the job). ASCII only.
set -uo pipefail
REPO=/opt/hippocampus/git-memory
APP=/opt/hippocampus/dream

echo "== [0] pre-state =="
PRE_HEAD=$(sudo -n git -C "$REPO" rev-parse HEAD); echo "pre HEAD=$PRE_HEAD"
PRE_STATUS=$(sudo -n git -C "$REPO" status --porcelain | wc -l); echo "pre dirty lines=$PRE_STATUS"

echo "== [1] tunnel unit + engine =="
systemctl is-active dream-engine-tunnel.service
curl -sS -m 10 http://127.0.0.1:18888/v1/models || echo "ENGINE UNREACHABLE"

echo "== [2] run the job once (dry-run) =="
sudo -n systemctl start dream.service
sleep 2
sudo -n systemctl show dream.service -p ExecMainStatus -p Result

echo "== [3] artifacts =="
ls -la "$APP/reports" | tail -3
ls -la "$APP/logs" | tail -3
TODAY=$(date +%F)
echo "--- report head ---"; sed -n '1,40p' "$APP/reports/$TODAY.md" 2>/dev/null || echo "no report for $TODAY"

echo "== [4] repo zero-change check =="
POST_HEAD=$(sudo -n git -C "$REPO" rev-parse HEAD); echo "post HEAD=$POST_HEAD"
POST_STATUS=$(sudo -n git -C "$REPO" status --porcelain | wc -l); echo "post dirty lines=$POST_STATUS"
if [ "$PRE_HEAD" = "$POST_HEAD" ] && [ "$PRE_STATUS" = "$POST_STATUS" ]; then echo "REPO-ZERO-CHANGE: PASS"; else echo "REPO-ZERO-CHANGE: FAIL"; fi

echo "== [5] timer schedule =="
systemctl list-timers dream.timer --no-pager | head -3

echo "== [6] fail-soft test (engine down) =="
sudo -n systemctl stop dream-engine-tunnel.service
sleep 2
sudo -n systemctl start dream.service; RC=$?
echo "job rc=$RC"
sudo -n systemctl show dream.service -p ExecMainStatus -p Result
echo "--- log tail ---"; tail -5 "$APP/logs/$(date +%F).log" 2>/dev/null
echo "--- report still produced? ---"; ls -la "$APP/reports/$(date +%F).md" 2>/dev/null && echo "REPORT-OK(fail-soft)" || echo "REPORT-MISSING"
echo "== [7] restore tunnel =="
sudo -n systemctl start dream-engine-tunnel.service
sleep 2; systemctl is-active dream-engine-tunnel.service
curl -sS -m 10 -o /dev/null -w 'engine http=%{http_code}\n' http://127.0.0.1:18888/v1/models || echo "ENGINE UNREACHABLE"
echo "== verify done =="
