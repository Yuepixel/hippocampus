#!/usr/bin/env bash
# dream-4c3-verify.sh - 4c-3 acceptance probe on 102: apply run -> exactly ONE commit. ASCII only.
# Repo is user-owned now, so plain `git` is used (no sudo -> avoids root dubious-ownership).
set -uo pipefail
REPO=/opt/hippocampus/git-memory
APP=/opt/hippocampus/dream
TODAY=$(date +%F)

echo "== [0] pre-state =="
PRE_HEAD=$(git -C "$REPO" rev-parse HEAD); echo "pre HEAD=$PRE_HEAD"
echo "pre dirty lines=$(git -C "$REPO" status --porcelain | wc -l)"

echo "== [1] engine reachable (tunnel) =="
systemctl is-active dream-engine-tunnel.service
curl -sS -m 10 http://127.0.0.1:18888/v1/models | head -c 200; echo

echo "== [2] apply run (systemctl start dream.service) =="
sudo -n systemctl start dream.service
sudo -n systemctl show dream.service -p ExecMainStatus -p Result

echo "== [3] exactly-one-commit check =="
POST_HEAD=$(git -C "$REPO" rev-parse HEAD); echo "post HEAD=$POST_HEAD"
CNT=$(git -C "$REPO" rev-list --count "$PRE_HEAD..$POST_HEAD")
echo "commits since pre = $CNT (expect 1)"
git -C "$REPO" log --oneline -1
git -C "$REPO" diff --stat "$PRE_HEAD..$POST_HEAD"
if [ "$CNT" = "1" ]; then echo "EXACTLY-ONE-COMMIT: PASS"; else echo "EXACTLY-ONE-COMMIT: FAIL"; fi

echo "== [4] report landed in repo (dreams/) + observability copy =="
ls -la "$REPO/dreams" | tail -3
ls -la "$APP/reports/$TODAY.md" 2>/dev/null || echo "no /opt reports copy"

echo "== [5] worktree clean after apply =="
echo "post dirty lines=$(git -C "$REPO" status --porcelain | wc -l)"

echo "== [6] log tail =="
tail -8 "$APP/logs/dream-$TODAY.log" 2>/dev/null

echo "== [7] timer =="
systemctl is-enabled dream.timer; systemctl list-timers dream.timer --no-pager | head -3
echo "== verify done =="
