#!/usr/bin/env bash
# dream-4c3-rollback.sh - rollback drill: stop the timer, git-revert the dream commit, re-enable. ASCII only.
# Usage: bash dream-4c3-rollback.sh [<commit>]   (default: HEAD == the latest dream commit)
set -uo pipefail
REPO=/opt/hippocampus/git-memory
TIMER=dream.timer
SHA=${1:-HEAD}
GIT="git -c user.name=hippocampus-dreamer -c user.email=dream@hippocampus.local -C $REPO"

echo "== [1] stop the timer (systemctl disable --now) =="
sudo -n systemctl disable --now "$TIMER"
echo "is-enabled: $(systemctl is-enabled "$TIMER" 2>&1)"; echo "is-active: $(systemctl is-active "$TIMER" 2>&1)"

echo "== [2] revert the dream commit =="
BEFORE=$($GIT rev-parse HEAD)
echo "target: $($GIT log -1 --format='%H %s' "$SHA")"
$GIT revert --no-edit "$SHA"
AFTER=$($GIT rev-parse HEAD)
echo "HEAD: $BEFORE -> $AFTER"

echo "== [3] content restored? =="
$GIT log --oneline -4
echo "dirty lines=$($GIT status --porcelain | wc -l)"

echo "== [4] re-enable the timer =="
sudo -n systemctl enable --now "$TIMER"
echo "is-enabled: $(systemctl is-enabled "$TIMER")"; echo "is-active: $(systemctl is-active "$TIMER")"
systemctl list-timers "$TIMER" --no-pager | head -3
echo "== rollback drill done =="
