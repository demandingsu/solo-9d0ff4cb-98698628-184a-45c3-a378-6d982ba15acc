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

step "11. 跨点串表：导入 MP-004（表号 M100 与 MP-001 重叠 08:00-14:00，14:00 持换表单 M100->M600）"
post_csv "$BASE/api/imports/readings" "$SAMPLES/readings_mp004.csv" | $PP
post_csv "$BASE/api/imports/meter-changes" "$SAMPLES/meter_changes_mp004.csv" | $PP
echo "-- 跨点核查：M100 在 MP-001 与 MP-004 的重叠时段、计量点与读数/单据来源"
get "$BASE/api/meter-cross-checks" | $PP
echo "-- 有效计量段（缺整点会断段；换表单边界读数也是段端点来源）"
get "$BASE/api/meter-segments?meter=M100" | $PP

step "12. 按当前数据重新判定：MP-001 2026-01-09 由确定变为待核（跨点串表，不给确定日总量）"
get "$BASE/api/settlement/daily?mp=MP-001&date=2026-01-09" | $PP
echo "-- MP-004 同日同样待核；重叠 6 小时的增量 delta_kwh=null"
get "$BASE/api/settlement/daily?mp=MP-004&date=2026-01-09" | $PP
echo "-- 第二实例在同一已提交快照上看到同一核查结果"
get "$BASE2/api/meter-cross-checks?meter=M100" | $PP || echo "(第二实例未启动可忽略)"

step "13. 不报冲突的两种情形：先后同表号仅端点相接 + 缺整点断段（MP-005，自 2026-01-10T00:00 起用 M100，14:00 缺整点）"
post_csv "$BASE/api/imports/readings" "$SAMPLES/readings_mp005.csv" | $PP
echo "-- 仍只有 MP-001/MP-004 一条冲突；MP-005 的 M100 段在 14:00 断开且不推定占用"
get "$BASE/api/meter-cross-checks" | $PP
get "$BASE/api/meter-segments?mp=MP-005" | $PP

step "14. 过滤用法：按计量点 / 表号 / 日期窗"
get "$BASE/api/meter-cross-checks?mp=MP-004&from=2026-01-09&to=2026-01-09" | $PP
get "$BASE/api/meter-cross-checks?mp=MP-005" | $PP

printf '\n\033[1;32m演示完成。Swagger UI: %s/docs\033[0m\n' "$BASE"
