#!/usr/bin/env bash
# brainstem-consumers-verify.sh - acceptance probe for C-S3 (brainstem consumers) on 102. ASCII only.
# Mostly read-only. Step [4] uses a throwaway sandbox under /tmp. Step [5] injects ONE controlled
# synthetic S4 signal into the live bus (name=selftest-live) to prove the .path wiring end-to-end.
set -uo pipefail
APP=/opt/hippocampus/brainstem-consumers
BS=/opt/hippocampus/brainstem
TODAY=$(date '+%Y-%m-%d')
REPORT="$APP/reports/health-$TODAY.md"

echo "== [1] units =="
echo "s3.path active=$(systemctl is-active brainstem-s3-health.path 2>&1) enabled=$(systemctl is-enabled brainstem-s3-health.path 2>&1)"
echo "s4.path active=$(systemctl is-active brainstem-s4-health.path 2>&1) enabled=$(systemctl is-enabled brainstem-s4-health.path 2>&1)"

echo "== [2] reports dir =="
ls -l "$APP/reports" 2>/dev/null || echo "(no reports yet)"
echo "--- tail today's report ---"
tail -20 "$REPORT" 2>/dev/null || echo "(no report for today yet)"

echo "== [3] consumer log =="
tail -10 "$APP/logs/consumer-health.log" 2>/dev/null || echo "(no consumer log yet)"

echo "== [4] sandbox logic test (s3 + s4 both directions; /tmp only) =="
SB=$(mktemp -d /tmp/hcc-XXXX)
mkdir -p "$SB/root/signals/s3" "$SB/root/signals/s4" "$SB/out" "$SB/logs"
printf '%s' '{"type":"s3","ts":"2026-10-16T20:30:01+08:00","health":"ok","down":[]}' > "$SB/root/signals/s3/current.json"
printf '%s' '{"type":"s4","ts":"2026-10-16T20:31:02+08:00","event":"transition","name":"gateway-101","from":"up","to":"down"}' > "$SB/root/signals/s4/current.json"
/usr/bin/python3 "$APP/health_report.py" --root "$SB/root" --out "$SB/out" --log "$SB/logs/c.log" >/dev/null 2>&1; rc=$?
echo "sandbox run rc=$rc (expect 0)"
echo "s3 cleared: $([ -f "$SB/root/signals/s3/current.json" ] && echo NO || echo YES) (expect YES)"
echo "s4 cleared: $([ -f "$SB/root/signals/s4/current.json" ] && echo NO || echo YES) (expect YES)"
echo "--- sandbox report ---"; cat "$SB/out/health-2026-10-16.md" 2>/dev/null
printf '%s' '{"type":"s4","ts":"2026-10-16T20:32:03+08:00","event":"transition","name":"gateway-101","from":"down","to":"up"}' > "$SB/root/signals/s4/current.json"
/usr/bin/python3 "$APP/health_report.py" --root "$SB/root" --out "$SB/out" --log "$SB/logs/c.log" >/dev/null 2>&1
echo "reverse direction last line:"; tail -1 "$SB/out/health-2026-10-16.md"
rm -rf "$SB"

echo "== [5] live wiring: controlled synthetic S4 (labelled selftest) =="
mkdir -p "$BS/signals/s4"
printf '%s' "{\"type\":\"s4\",\"ts\":\"$(date '+%Y-%m-%dT%H:%M:%S%:z')\",\"event\":\"transition\",\"name\":\"selftest-live\",\"from\":\"up\",\"to\":\"down\"}" > "$BS/signals/s4/current.json"
echo "injected; waiting for the path unit..."
sleep 3
echo "s4 signal cleared: $([ -f "$BS/signals/s4/current.json" ] && echo NO || echo YES) (expect YES)"
echo "--- report tail ---"; tail -3 "$REPORT" 2>/dev/null

echo "== [6] boundary self-check (must not touch mem0 / git-memory / request path) =="
HITS=$(grep -c -E ':8890|git commit|v1/chat|curl|18888|8888' "$APP/health_report.py" 2>/dev/null || true)
echo "mem0/git/request-path hits: ${HITS:-0} (expect 0)"

echo "== [7] brainstem untouched (pure subscriber) =="
echo "brainstem.service active=$(systemctl is-active brainstem.service)"
echo "signals tree:"; find "$BS/signals" -type f 2>/dev/null | sort | tail -12

echo "== [8] C-S2 (idle info collection) live state =="
echo "s2.path active=$(systemctl is-active brainstem-s2-collect.path 2>&1) enabled=$(systemctl is-enabled brainstem-s2-collect.path 2>&1)"
echo "collect.service active=$(systemctl is-active brainstem-collect.service 2>&1)"
echo "--- collect report tail ---"
tail -25 "$APP/reports/collect-$TODAY.md" 2>/dev/null || echo "(no collect report for today yet)"
echo "--- collect consumer log tail ---"
tail -10 "$APP/logs/consumer-collect.log" 2>/dev/null || echo "(no collect log yet)"
echo "--- s2 state ---"
cat "$APP/state/consumer-s2.json" 2>/dev/null || echo "(no state yet)"
echo "--- s2 signal (should be gone once consumed) ---"
ls -l "$BS/signals/s2" 2>/dev/null || echo "(no s2 dir)"
echo "--- engine reachability (loopback tunnel) ---"
curl -s -o /dev/null -w 'v1/models=%{http_code}\n' --max-time 10 http://127.0.0.1:18888/v1/models

echo "== [9] C-S2 sandbox logic test (closed port -> fail-soft; rate limit; zero reports elsewhere) =="
SB2=$(mktemp -d /tmp/hcc2-XXXX)
mkdir -p "$SB2/root/signals/s2" "$SB2/signals/s2" "$SB2/gm" "$SB2/state" "$SB2/logs"
printf '%s' '{"type":"s2","ts":"2026-10-16T20:12:38+08:00","idle_seconds":600.0,"loadavg":0.18}' > "$SB2/root/signals/s2/current.json"
/usr/bin/python3 "$APP/collect_report.py" --root "$SB2/root" --out "$SB2/signals" --gm "$SB2/gm" --state "$SB2/state" --log "$SB2/logs/c.log" --base-url http://127.0.0.1:9/v1 --timeout 5 >/dev/null 2>&1; rc=$?
echo "sandbox fail-soft run rc=$rc (expect 0)"
echo "s2 signal cleared: $([ -f "$SB2/root/signals/s2/current.json" ] && echo NO || echo YES) (expect YES)"
echo "--- sandbox report (expect a 'skip / engine unreachable' section, no crash) ---"
cat "$SB2/signals/collect-2026-10-16.md" 2>/dev/null || echo "(no report)"
printf '%s' '{"type":"s2","ts":"2026-10-16T21:00:00+08:00","idle_seconds":900.0,"loadavg":0.2}' > "$SB2/root/signals/s2/current.json"
/usr/bin/python3 "$APP/collect_report.py" --root "$SB2/root" --out "$SB2/signals" --gm "$SB2/gm" --state "$SB2/state" --log "$SB2/logs/c.log" --base-url http://127.0.0.1:9/v1 --timeout 5 --min-interval-hours 6 >/dev/null 2>&1
echo "second run within interval -> last section:"
tail -5 "$SB2/signals/collect-2026-10-16.md" 2>/dev/null | grep -E '限频|跳过' || echo "(expected a rate-limit skip section)"
rm -rf "$SB2"

echo "== [10] C-S2 boundary self-check (no mem0 / git write / external egress / tools) =="
HITS2=$(grep -c -E ':8890|git commit|git push|tool_calls|curl|https://' "$APP/collect_report.py" 2>/dev/null || true)
echo "mem0/git-write/external/tools hits: ${HITS2:-0} (expect 0)"
echo "loopback-only egress (all http:// must be the tunnel):"
grep -o -E 'https?://[^ ")]+' "$APP/collect_report.py" | sort -u

echo "== [11] zero side effects on git-memory =="
echo "git-memory HEAD=$(git -C /opt/hippocampus/git-memory rev-parse --short HEAD) dirty=$(git -C /opt/hippocampus/git-memory status --porcelain=v1 | wc -l)"

echo "== verify done =="
