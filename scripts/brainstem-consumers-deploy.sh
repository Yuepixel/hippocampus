#!/usr/bin/env bash
# brainstem-consumers-deploy.sh - install the brainstem consumers (C-S3 health + C-S2 collect) on 102
# (run as user; sudo -n). ASCII only.
# Expects staged payload at /tmp/brainstem-consumers-stage/{health_report.py,collect_report.py,tests/,units/}
# Does NOT touch brainstem itself (pure subscriber: reads signals/, writes its own reports/).
set -euo pipefail
APP=/opt/hippocampus/brainstem-consumers
STAGE=/tmp/brainstem-consumers-stage

echo "== stage check =="
ls -1 "$STAGE" || { echo "FATAL: $STAGE missing"; exit 1; }
[ -f "$STAGE/health_report.py" ] || { echo "FATAL: health_report.py missing"; exit 1; }
[ -f "$STAGE/collect_report.py" ] || { echo "FATAL: collect_report.py missing"; exit 1; }

echo "== install app dir =="
sudo -n mkdir -p "$APP" "$APP/reports" "$APP/logs" "$APP/state"
sudo -n cp "$STAGE/health_report.py" "$APP/health_report.py"
sudo -n cp "$STAGE/collect_report.py" "$APP/collect_report.py"
if [ -d "$STAGE/tests" ]; then
  sudo -n rm -rf "$APP/tests"; sudo -n cp -r "$STAGE/tests" "$APP/tests"
fi
sudo -n chown -R user:user "$APP"
python3 -c "import ast; ast.parse(open('$APP/health_report.py',encoding='utf-8').read()); print('health_report.py syntax OK')"
python3 -c "import ast; ast.parse(open('$APP/collect_report.py',encoding='utf-8').read()); print('collect_report.py syntax OK')"

echo "== run unit tests on 102 =="
cd "$APP"
PY=/opt/hippocampus/venv/bin/python
[ -x "$PY" ] || PY=python3
echo "using interpreter: $PY"
"$PY" -m unittest discover -s tests 2>&1 | grep -E 'Ran [0-9]+ tests|^OK$|^FAILED'

echo "== install systemd units =="
sudo -n cp "$STAGE"/units/brainstem-health.service /etc/systemd/system/
sudo -n cp "$STAGE"/units/brainstem-s3-health.path /etc/systemd/system/
sudo -n cp "$STAGE"/units/brainstem-s4-health.path /etc/systemd/system/
sudo -n cp "$STAGE"/units/brainstem-collect.service /etc/systemd/system/
sudo -n cp "$STAGE"/units/brainstem-s2-collect.path /etc/systemd/system/
sudo -n systemctl daemon-reload

echo "== enable + start the C-S3 (S3/S4) path watchers =="
sudo -n systemctl enable --now brainstem-s3-health.path brainstem-s4-health.path

echo "== enable + start the C-S2 (S2) path watcher =="
# NOTE: if signals/s2/current.json already exists, this fires brainstem-collect.service at once
# (that is the intended live acceptance path); otherwise it simply re-arms.
sudo -n systemctl enable --now brainstem-s2-collect.path

echo "== status =="
echo "brainstem-s3-health.path active=$(systemctl is-active brainstem-s3-health.path) enabled=$(systemctl is-enabled brainstem-s3-health.path)"
echo "brainstem-s4-health.path active=$(systemctl is-active brainstem-s4-health.path) enabled=$(systemctl is-enabled brainstem-s4-health.path)"
echo "brainstem-s2-collect.path active=$(systemctl is-active brainstem-s2-collect.path) enabled=$(systemctl is-enabled brainstem-s2-collect.path)"
echo "brainstem.service (must be unchanged) active=$(systemctl is-active brainstem.service)"
echo "reports:"; ls -l "$APP/reports" 2>/dev/null || true
echo "== deploy done =="
