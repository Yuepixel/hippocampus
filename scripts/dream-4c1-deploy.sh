#!/usr/bin/env bash
# dream-4c1-deploy.sh - install the dream job on 102 (run as user; uses sudo -n). ASCII only.
# Expects staged payload at /tmp/dream-stage/{dream.py,tests/,units/}
set -euo pipefail
APP=/opt/hippocampus/dream
STAGE=/tmp/dream-stage

echo "== stage check =="
ls -1 "$STAGE" || { echo "FATAL: $STAGE missing"; exit 1; }
[ -f "$STAGE/dream.py" ] || { echo "FATAL: dream.py missing"; exit 1; }

echo "== install app dir =="
sudo -n mkdir -p "$APP" "$APP/reports" "$APP/logs"
sudo -n cp "$STAGE/dream.py" "$APP/dream.py"
if [ -d "$STAGE/tests" ]; then
  sudo -n rm -rf "$APP/tests"; sudo -n cp -r "$STAGE/tests" "$APP/tests"
fi
sudo -n chown -R user:user "$APP"
python3 -c "import ast,sys; ast.parse(open('$APP/dream.py',encoding='utf-8').read()); print('dream.py syntax OK')"

echo "== install systemd units =="
sudo -n cp "$STAGE"/units/dream-engine-tunnel.service /etc/systemd/system/
sudo -n cp "$STAGE"/units/dream.service /etc/systemd/system/
sudo -n cp "$STAGE"/units/dream.timer /etc/systemd/system/
sudo -n systemctl daemon-reload

echo "== enable + start tunnel, enable timer =="
sudo -n systemctl enable --now dream-engine-tunnel.service
sudo -n systemctl enable --now dream.timer

echo "== status =="
systemctl is-active dream-engine-tunnel.service; systemctl is-enabled dream.timer; systemctl is-enabled dream-engine-tunnel.service
systemctl list-timers dream.timer --no-pager | head -3
echo "== tunnel reachability =="
curl -sS -m 10 http://127.0.0.1:18888/v1/models || echo "ENGINE UNREACHABLE"
echo
echo "== deploy done =="
