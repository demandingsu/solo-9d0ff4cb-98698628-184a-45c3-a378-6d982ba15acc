#!/usr/bin/env bash
# 端到端演示：跨午夜换表、迟到单据补录重算、缺整点/倒退/边界冲突待核、失败导入回滚。
# 用法: ./scripts/demo.sh [BASE_URL]   默认 http://localhost:8001
set -euo pipefail

BASE="${1:-${BASE_URL:-http://localhost:8001}}"
BASE2="${BASE2:-http://localhost:8002}"
SAMPLES="$(cd "$(dirname "$0")/.." && pwd)/samples"

if command -v jq >/dev/null 2>&1; then PP="jq ."; else PP="python3 -m json.tool"; fi

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
post_csv() { curl -sS -X POST "$1" -H "accept: application/json" -F "file=@$2;type=text/csv"; }
get() { curl -sS "$1"; }

step "0. 健康检查 ($BASE / $BASE2)"
get "$BASE/api/health" | $PP
get "$BASE2/api/health" | $PP || echo "(第二实例未启动可忽略)"

step "1. 导入 MP-001 读数（跨午夜换表 M100->M200 @2026-01-10T00:00Z，换表单尚未到）"
post_csv "$BASE/api/imports/readings" "$SAMPLES/readings_mp001.csv" | $PP

step "2. 核算 2026-01-09：午夜换表无单据 -> 待核区间，不给确定日总量"
get "$BASE/api/settlement/daily?mp=MP-001&date=2026-01-09" | $PP

step "3. 迟到换表单补录（旧表末读数 1240.0 / 新表初值 40.0），返回受影响日期"
post_csv "$BASE/api/imports/meter-changes" "$SAMPLES/meter_changes_mp001_late.csv" | $PP

step "4. 重算 2026-01-09：旧表末段 1230->1240 补上，日总量确定 = 240.0"
get "$BASE/api/settlement/daily?mp=MP-001&date=2026-01-09" | $PP

step "5. 核算 2026-01-10（换表后第一天，全新表）：日总量 = 120.0"
get "$BASE/api/settlement/daily?mp=MP-001&date=2026-01-10" | $PP

step "6. 第二实例读取同一已提交状态（$BASE2）"
get "$BASE2/api/settlement/daily?mp=MP-001&date=2026-01-09" | $PP || echo "(第二实例未启动可忽略)"

step "7. 导入 MP-002：同表倒退(05:00->06:00) + 缺整点(14:00) -> 两段待核区间"
post_csv "$BASE/api/imports/readings" "$SAMPLES/readings_mp002.csv" | $PP
get "$BASE/api/settlement/daily?mp=MP-002&date=2026-01-09" | $PP

step "8. 导入 MP-003：日中换表，单据新表初值 100 与常规读数 105 冲突 -> 边界读数冲突待核"
post_csv "$BASE/api/imports/readings" "$SAMPLES/readings_mp003.csv" | $PP
post_csv "$BASE/api/imports/meter-changes" "$SAMPLES/meter_changes_mp003.csv" | $PP
get "$BASE/api/settlement/daily?mp=MP-003&date=2026-01-09" | $PP

step "9. 失败导入回滚：含冲突读数的文件 -> 409，合法行也不入库，已有结果不变"
curl -sS -o /tmp/conflict_resp.json -w "HTTP %{http_code}\n" \
  -X POST "$BASE/api/imports/readings" -F "file=@$SAMPLES/readings_mp001_conflict.csv;type=text/csv"
cat /tmp/conflict_resp.json | $PP
echo "-- 文件中的合法行 2026-01-12T00:00:00Z 不应入库（空结果）:"
get "$BASE/api/readings?mp=MP-001&from=2026-01-12&to=2026-01-12" | $PP
echo "-- 2026-01-09 核算结果保持不变:"
get "$BASE/api/settlement/daily?mp=MP-001&date=2026-01-09" | $PP

step "10. 导入批次与计量点总览"
get "$BASE/api/imports" | $PP
get "$BASE/api/metering-points" | $PP

printf '\n\033[1;32m演示完成。Swagger UI: %s/docs\033[0m\n' "$BASE"
