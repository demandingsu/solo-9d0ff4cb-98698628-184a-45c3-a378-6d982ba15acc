# 园区能源日用电量核算 API

把逐小时电表**累计读数**换算成计量点的**日用电量**（UTC 日界），供结算使用。
FastAPI + SQLite，支持换表（含迟到换表单补录）、异常数据返回待核区间、
**跨点串表核查**（同一表号被归入两个计量点时的重叠时段核查）、
两个 API 实例并发导入与查询。

## 快速开始

```bash
docker compose up --build
# api1: http://localhost:8001/docs
# api2: http://localhost:8002/docs   （两个实例共享同一 SQLite 库）
```

启动后跑一遍完整演示（跨午夜换表、迟到单据补录重算、倒退/缺整点/边界冲突、失败导入回滚、
跨点串表冲突与不重叠/缺整点不报）：

```bash
./scripts/demo.sh                  # 默认打 http://localhost:8001，同时验证 8002
```

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
DB_PATH=./energy.db uvicorn app.main:app --port 8001
pytest tests/ -q
```

## 核算规则

- 结算日为 **UTC 日界**：某日用电量 = 相邻两个 UTC 午夜之间 24 个整点增量之和，
  需要当日 `00:00` 到次日 `00:00` 共 25 个整点读数。
- **换表整点 T**：换表单给出旧表末读数与新表初值（边界读数），可代替该整点的常规读数；
  `T-1→T` 为旧表末段、`T→T+1` 为新表首段，分别计算后相加。换表发生在午夜时，
  末段计入前一日、首段计入当日，天然支持跨午夜换表。
- 以下情况**不给确定日总量**（`total_kwh=null`），只返回**待核区间** `pending_intervals`
  （`known_kwh` 为可确定部分的电量，仅供参考）：

  | reason | 含义 |
  |---|---|
  | `missing_hour` | 缺整点读数 |
  | `unrecorded_meter_change` | 相邻整点表号切换但无换表单 |
  | `meter_regression` | 同表累计读数倒退 |
  | `boundary_conflict` | 常规读数与换表单边界读数（或表号）冲突 |
  | `cross_meter_conflict` | 该小时同一表号在其它计量点的有效计量段与之重叠（跨点串表） |

- **迟到单据**：核算为按需实时计算。补录换表单后，导入响应会给出 `affected_dates`
  （受影响结算日），重新查询这些日期即得重算结果，无需手工触发重算。

## 跨点串表核查

同一物理表号被归入两个计量点时，两处单独核算都可能显示“确定用量”，但该表在重叠
时段的用量归属无法确定。核查依据**相邻整点读数**与**换表边界读数**形成的**有效计量段**：

- 段内每个整点都有该表的有效数据点（常规读数或换表单的旧表末读数/新表初值）；
  相邻整点构成一个占用小时。**缺整点即断段，缺小时不推定占用**。
- 换表整点 `T`：旧表段在 `T` 闭端结束、新表段自 `T` 开端开始（段按半开区间 `[start, end)` 表示）。
- 同一表号在**不同计量点**的段出现**正长度时段重叠**才报冲突；仅端点相接（先后使用、
  段不重叠）不报。每条冲突返回重叠时段、涉及计量点、参与计量段，以及逐整点的
  **读数或换表单来源**（含 `import_id` 与来源文件名）。
- 与冲突时段相交的**结算日一律标待核**：日核算 `status=pending`、`total_kwh=null`，
  受影响小时的增量 `delta_kwh=null` 并带 `cross_meter_conflict` 原因与来源说明，
  结构化记录同时出现在响应的 `cross_meter_conflicts` 字段；`known_kwh` 只统计
  不受影响的可确定小时。
- **按当前数据实时判定**：补录读数或换表单后重新查询/核查即重新判断——补点可能使段
  前延而新产生冲突，补齐依据或修正归属后冲突也可能消失。
- 核查与日核算在**同一个只读事务快照**内完成（见下节），本点用量与跨点冲突取自
  同一已提交状态；失败导入整体回滚，不改变任何核查/核算结果。

## CSV 格式

读数 CSV（`POST /api/imports/readings`，multipart 文件字段名 `file`）：

```csv
mp,meter,ts,cum_kwh
MP-001,M100,2026-01-09T00:00:00Z,1000.0
```

换表单 CSV（`POST /api/imports/meter-changes`）：

```csv
mp,change_ts,old_meter,new_meter,old_final_kwh,new_initial_kwh
MP-001,2026-01-10T00:00:00Z,M100,M200,1240.0,40.0
```

- `ts`/`change_ts` 必须是 **UTC 整点**（可带时区偏移，会归一化为 UTC，但必须落在整点）。
- 整批原子导入：任一行校验失败（422）或与已提交数据冲突（409）→ 整批回滚，已有结果不变。
- 同值重复导入幂等（计 `duplicates_skipped`），不同值视为冲突。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/imports/readings` | 导入读数 CSV |
| POST | `/api/imports/meter-changes` | 导入换表单 CSV（可迟到补录） |
| GET | `/api/settlement/daily?mp=&date=` | 单日核算（确定总量或待核区间，含跨点串表待核） |
| GET | `/api/settlement/range?mp=&from=&to=` | 区间核算（同一已提交快照，≤63 天） |
| GET | `/api/meter-cross-checks?mp=&meter=&from=&to=` | 跨点串表核查（同表号跨计量点的重叠时段与来源） |
| GET | `/api/meter-segments?mp=&meter=&from=&to=` | 有效计量段与来源查看（核查判定依据） |
| GET | `/api/metering-points` | 计量点总览 |
| GET | `/api/readings?mp=&from=&to=` | 读数查看 |
| GET | `/api/meter-changes?mp=` | 换表单查看 |
| GET | `/api/imports` | 导入批次列表 |
| GET | `/api/health` | 健康检查 |

交互式文档见 `/docs`（Swagger UI，可直接上传 `samples/` 里的 CSV 演示）。

## 并发与一致性设计

两个 API 实例共享同一 SQLite 库文件（compose 中的 named volume）：

- **WAL 模式**：核算查询与跨点串表核查都在显式只读事务（`BEGIN … COMMIT`）内执行，
  读事务看到一致的已提交快照——一次核算/核查只读同一已提交状态，日核算中的本点用量
  与跨点冲突取自同一快照，即使另一实例正在导入也不会读到半截批次。响应中的
  `snapshot.max_import_id` 即本次判定所读快照的批次水位。
- **原子导入**：每批导入在单个 `BEGIN IMMEDIATE` 事务内校验+写入，
  任何失败整体 `ROLLBACK`，已有核算/核查结果不变；`busy_timeout` 吸收两实例间的写锁竞争。
- 失败导入不留痕迹（`imports` 表也只有已提交批次）。

`tests/test_app.py` 含多线程并发导入+快照读的一致性回归测试，以及跨点串表的
重叠/不重叠/缺整点/单据来源/补录重判/失败回滚等端到端用例。

## 样例数据（samples/）

| 文件 | 场景 |
|---|---|
| `readings_mp001.csv` | MP-001 跨午夜换表（2026-01-10T00:00Z M100→M200），单据未到 |
| `meter_changes_mp001_late.csv` | MP-001 迟到的换表单（旧表末读数 1240.0 / 新表初值 40.0） |
| `readings_mp001_conflict.csv` | 与已提交读数冲突的文件（演示 409 + 整批回滚） |
| `readings_mp002.csv` | MP-002 同表倒退（05:00→06:00）+ 缺整点（14:00） |
| `readings_mp003.csv` + `meter_changes_mp003.csv` | MP-003 日中换表，单据新表初值与常规读数冲突 |
| `readings_mp004.csv` + `meter_changes_mp004.csv` | MP-004 同表号 **M100 与 MP-001 在 08:00–14:00 重叠**（跨点串表），14:00 持换表单 M100→M600，段端点含单据来源 |
| `readings_mp005.csv` | MP-005 自 2026-01-10T00:00 起用 M100（与 MP-001 仅端点相接，**不报冲突**），且 14:00 缺整点断段 |

演示脚本 `scripts/demo.sh` 的期望结果：MP-001 补录单据前 2026-01-09 为待核
（`unrecorded_meter_change`，`known_kwh=230`），补录后重算 `total_kwh=240`，
次日 `total_kwh=120`；MP-002 两段待核区间、`known_kwh=168`；
MP-003 待核区间 11:00–13:00、`known_kwh=385`。
导入 MP-004 后跨点核查返回 **M100 在 MP-001 与 MP-004 的 08:00–14:00 重叠**（6 小时，
来源含读数与 MP-004 换表单旧表末读数），MP-001 与 MP-004 的 2026-01-09 均改为待核、
不给确定日总量（MP-001 `known_kwh=180`）；MP-005 不产生任何冲突，其 M100 段在
缺整点处断为 `[00:00,13:00)` 与 `[15:00,24:00)` 两段。

## 目录结构

```
app/
  main.py     # FastAPI 路由、导入事务、查询端点
  calc.py     # 日核算核心（整点增量、换表分段、待核区间合并、跨点串表待核）
  segments.py # 跨点串表核查（有效计量段、重叠检测、读数/单据来源、快照水位）
  parsing.py  # CSV 解析与校验
  db.py       # SQLite 连接与 schema（WAL）
samples/      # 演示 CSV（含跨点串表 MP-004 / 不重叠断段 MP-005）
scripts/demo.sh
scripts/_verify_stdlib.py   # 无第三方依赖时的标准库验证脚本（python3 -I 运行）
tests/test_app.py
Dockerfile / docker-compose.yml
```
