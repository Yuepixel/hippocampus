#!/usr/bin/env bash
# 短期池 → 长期池 沉淀（由 pool-consolidate.timer 每日 04:30 调用）
# token 从 gateway_config.json 读取，不写死在脚本里。
set -u
CFG=/opt/hippocampus/server/gateway_config.json
TOK=$(python3 -c "import json;print(json.load(open('$CFG')).get('bearerToken') or '')" 2>/dev/null)
LIMIT="${1:-20}"
curl -s --max-time 900 -X POST \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TOK}" \
  -d "{\"dry_run\":false,\"limit\":${LIMIT}}" \
  http://127.0.0.1:8890/api/pool/consolidate
echo
