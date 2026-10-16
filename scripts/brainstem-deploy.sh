#!/usr/bin/env bash
# brainstem-deploy.sh - install the brainstem (fourth organ) on 102 (run as user; sudo -n). ASCII only.
# Expects staged payload at /tmp/brainstem-stage/{brainstem.py,tests/,units/,consume-s1-dream.sh}
# NOTE: does NOT hand over the dream trigger; run brainstem-dream-handover.sh on after verifying.
set -euo pipefail
APP=/opt/hippocampus/brainstem
STAGE=/tmp/brainstem-stage

echo "== stage check =="
ls -1 "$STAGE" || { echo "FATAL: $STAGE missing"; exit 1; }
[ -f "$STAGE/brainstem.py" ] || { echo "FATAL: brainstem.py missing"; exit 1; }

echo "== install app dir =="
sudo -n mkdir -p "$APP" "$APP/signals" "$APP/state" "$APP/logs" "$APP/inbox/reflex"
sudo -n cp "$STAGE/brainstem.py" "$APP/brainstem.py"
if [ -d "$STAGE/tests" ]; then
  sudo -n rm -rf "$APP/tests"; sudo -n cp -r "$STAGE/tests" "$APP/tests"
fi
sudo -n cp "$STAGE/consume-s1-dream.sh" "$APP/consume-s1-dream.sh"
sudo -n chmod +x "$APP/consume-s1-dream.sh"
sudo -n chown -R user:user "$APP"
python3 -c "import ast,sys; ast.parse(open('$APP/brainstem.py',encoding='utf-8').read()); print('brainstem.py syntax OK')"

echo "== run unit tests on 102 =="
cd "$APP"
PY=/opt/hippocampus/venv/bin/python
[ -x "$PY" ] || PY=python3
echo "using interpreter: $PY"
"$PY" -m unittest discover -s tests 2>&1 | grep -E 'Ran [0-9]+ tests|^OK$|^FAILED'

echo "== install systemd units =="
sudo -n cp "$STAGE"/units/brainstem.service /etc/systemd/system/
sudo -n cp "$STAGE"/units/brainstem-s1-dream.path /etc/systemd/system/
sudo -n cp "$STAGE"/units/brainstem-s1-dream.service /etc/systemd/system/
sudo -n systemctl daemon-reload

echo "== enable + start brainstem + S1 consumer (dream trigger NOT yet handed over) =="
sudo -n systemctl enable --now brainstem.service
sudo -n systemctl enable --now brainstem-s1-dream.path

echo "== status =="
echo "brainstem.service active: $(systemctl is-active brainstem.service)  enabled: $(systemctl is-enabled brainstem.service)"
echo "brainstem-s1-dream.path active: $(systemctl is-active brainstem-s1-dream.path)  enabled: $(systemctl is-enabled brainstem-s1-dream.path)"
echo "dream.timer enabled (should still be enabled until handover): $(systemctl is-enabled dream.timer 2>&1)"
echo "== deploy done =="
