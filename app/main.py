"""园区能源日用电量核算 API。

- POST /api/imports/readings        导入读数 CSV（计量点,表号,UTC整点,累计kWh）
- POST /api/imports/meter-changes   导入换表单 CSV（可迟到，补录后重算受影响日期）
- GET  /api/settlement/daily        单计量点单日核算（待核区间 / 确定日总量）
- GET  /api/settlement/range        日期区间核算（同一已提交快照）
- GET  /api/metering-points|readings|meter-changes|imports  数据查看
- GET  /api/health                  健康检查

一致性：导入单事务原子提交（失败整体回滚，已有结果不变）；核算在只读事务内
完成，WAL 快照保证一次核算只读同一已提交状态。核算按需实时计算，补录换表单
后直接重新查询受影响日期即得重算结果。
"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import date as date_type
from datetime import datetime, time, timedelta
from decimal import Decimal

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from . import db
from .calc import HOUR, INSTANCE, compute_day, fmt_ts
from .parsing import (ChangeRow, CsvError, ReadingRow, parse_meter_changes_csv,
                      parse_readings_csv)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="园区能源日用电量核算 API",
    version="1.0.0",
    description=(
        "逐小时电表累计读数 → 计量点日用电量（UTC 日界）。支持换表单（含迟到补录）、"
        "缺整点/无单据换表/同表倒退/边界读数冲突时返回待核区间。"
        "样例数据见仓库 samples/，演示脚本见 scripts/demo.sh。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(sqlite3.OperationalError)
async def sqlite_busy_handler(request: Request, exc: sqlite3.OperationalError):
    # 两个实例并发写时的兜底：busy_timeout 内仍未拿到写锁
    return JSONResponse(
        status_code=503,
        content={"detail": "数据库忙，请重试", "error": str(exc)},
    )


# ---------------------------------------------------------------- 导入

def _affected_dates(ts_list: list[str]) -> list[str]:
    """整点读数/换表影响相邻两个增量，因此影响前一日与当日两个结算日。"""
    days: set[str] = set()
    for ts in ts_list:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        days.add(t.date().isoformat())
        days.add((t - HOUR).date().isoformat())
    return sorted(days)


def _store_readings(rows: list[ReadingRow], source_name: str) -> dict:
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        import_id = conn.execute(
            "INSERT INTO imports (kind, source_name, row_count) VALUES ('readings', ?, ?)",
            (source_name, len(rows)),
        ).lastrowid
        inserted = skipped = 0
        for r in rows:
            existing = conn.execute(
                "SELECT cum_kwh FROM readings WHERE mp=? AND meter=? AND ts=?",
                (r.mp, r.meter, r.ts),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO readings (mp, meter, ts, cum_kwh, import_id) VALUES (?,?,?,?,?)",
                    (r.mp, r.meter, r.ts, float(r.value), import_id),
                )
                inserted += 1
            elif Decimal(str(existing["cum_kwh"])) == r.value:
                skipped += 1  # 幂等重传
            else:
                raise HTTPException(
                    status_code=409,
                    detail=(f"读数冲突: {r.mp}/{r.meter}/{r.ts} 已存在 "
                            f"{existing['cum_kwh']}，新值 {r.value}；整批已回滚"),
                )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    by_mp: dict[str, list[str]] = {}
    for r in rows:
        by_mp.setdefault(r.mp, []).append(r.ts)
    return {
        "import_id": import_id,
        "kind": "readings",
        "status": "committed",
        "source_name": source_name,
        "inserted": inserted,
        "duplicates_skipped": skipped,
        "affected_dates": {mp: _affected_dates(ts) for mp, ts in by_mp.items()},
    }


def _store_meter_changes(rows: list[ChangeRow], source_name: str) -> dict:
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        import_id = conn.execute(
            "INSERT INTO imports (kind, source_name, row_count) VALUES ('meter_changes', ?, ?)",
            (source_name, len(rows)),
        ).lastrowid
        inserted = skipped = 0
        for r in rows:
            existing = conn.execute(
                "SELECT * FROM meter_changes WHERE mp=? AND change_ts=?",
                (r.mp, r.change_ts),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO meter_changes (mp, change_ts, old_meter, new_meter,"
                    " old_final_kwh, new_initial_kwh, import_id)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (r.mp, r.change_ts, r.old_meter, r.new_meter,
                     float(r.old_final), float(r.new_initial), import_id),
                )
                inserted += 1
            elif (existing["old_meter"] == r.old_meter and existing["new_meter"] == r.new_meter
                  and Decimal(str(existing["old_final_kwh"])) == r.old_final
                  and Decimal(str(existing["new_initial_kwh"])) == r.new_initial):
                skipped += 1  # 幂等重传
            else:
                raise HTTPException(
                    status_code=409,
                    detail=(f"换表单冲突: {r.mp}@{r.change_ts} 已存在不同单据；整批已回滚"),
                )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    by_mp: dict[str, list[str]] = {}
    for r in rows:
        by_mp.setdefault(r.mp, []).append(r.change_ts)
    return {
        "import_id": import_id,
        "kind": "meter_changes",
        "status": "committed",
        "source_name": source_name,
        "inserted": inserted,
        "duplicates_skipped": skipped,
        # 补录单据后需要重算的日期
        "affected_dates": {mp: _affected_dates(ts) for mp, ts in by_mp.items()},
    }


@app.post("/api/imports/readings", summary="导入读数 CSV")
def import_readings(file: UploadFile = File(..., description="列: mp,meter,ts,cum_kwh")):
    try:
        rows = parse_readings_csv(file.file.read())
    except CsvError as e:
        raise HTTPException(status_code=422, detail={"msg": "CSV 校验失败，整批未导入", "errors": e.errors})
    if not rows:
        raise HTTPException(status_code=422, detail="CSV 没有有效数据行")
    return _store_readings(rows, file.filename or "")


@app.post("/api/imports/meter-changes", summary="导入换表单 CSV（支持迟到补录）")
def import_meter_changes(file: UploadFile = File(..., description="列: mp,change_ts,old_meter,new_meter,old_final_kwh,new_initial_kwh")):
    try:
        rows = parse_meter_changes_csv(file.file.read())
    except CsvError as e:
        raise HTTPException(status_code=422, detail={"msg": "CSV 校验失败，整批未导入", "errors": e.errors})
    if not rows:
        raise HTTPException(status_code=422, detail="CSV 没有有效数据行")
    return _store_meter_changes(rows, file.filename or "")


# ---------------------------------------------------------------- 核算

@app.get("/api/settlement/daily", summary="单日核算")
def settlement_daily(
    mp: str = Query(..., min_length=1, max_length=64, description="计量点"),
    date: date_type = Query(..., description="结算日期 (UTC)，如 2026-01-09"),
):
    conn = db.connect()
    try:
        conn.execute("BEGIN")  # 只读事务：WAL 快照，一次核算只读同一已提交状态
        try:
            result = compute_day(conn, mp, date)
        finally:
            conn.execute("COMMIT")
        return result
    finally:
        conn.close()


@app.get("/api/settlement/range", summary="日期区间核算（同一快照）")
def settlement_range(
    mp: str = Query(..., min_length=1, max_length=64),
    from_: date_type = Query(..., alias="from"),
    to: date_type = Query(...),
):
    if to < from_:
        raise HTTPException(status_code=422, detail="to 不能早于 from")
    if (to - from_).days > 62:
        raise HTTPException(status_code=422, detail="区间最长 63 天")
    conn = db.connect()
    try:
        conn.execute("BEGIN")
        try:
            days = []
            d = from_
            while d <= to:
                days.append(compute_day(conn, mp, d))
                d += timedelta(days=1)
        finally:
            conn.execute("COMMIT")
        return {"mp": mp, "days": days, "instance": INSTANCE}
    finally:
        conn.close()


# ---------------------------------------------------------------- 数据查看

@app.get("/api/health")
def health():
    conn = db.connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok", "instance": INSTANCE}


@app.get("/api/metering-points", summary="计量点列表")
def metering_points():
    conn = db.connect()
    try:
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT mp, COUNT(*) AS reading_count, MIN(ts) AS first_ts, MAX(ts) AS last_ts"
            " FROM readings GROUP BY mp ORDER BY mp").fetchall()
        forms = conn.execute(
            "SELECT mp, COUNT(*) AS c FROM meter_changes GROUP BY mp").fetchall()
        conn.execute("COMMIT")
    finally:
        conn.close()
    fc = {f["mp"]: f["c"] for f in forms}
    mps = {r["mp"] for r in rows} | set(fc)
    return [{
        "mp": mp,
        "reading_count": next((r["reading_count"] for r in rows if r["mp"] == mp), 0),
        "first_ts": next((r["first_ts"] for r in rows if r["mp"] == mp), None),
        "last_ts": next((r["last_ts"] for r in rows if r["mp"] == mp), None),
        "meter_change_count": fc.get(mp, 0),
    } for mp in sorted(mps)]


@app.get("/api/readings", summary="读数查看")
def list_readings(
    mp: str = Query(..., min_length=1, max_length=64),
    from_: date_type = Query(None, alias="from"),
    to: date_type = Query(None),
):
    lo = fmt_ts(datetime.combine(from_, time.min)) if from_ else "0000"
    hi = fmt_ts(datetime.combine(to + timedelta(days=1), time.min)) if to else "9999"
    conn = db.connect()
    try:
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT mp, meter, ts, cum_kwh, import_id FROM readings"
            " WHERE mp=? AND ts BETWEEN ? AND ? ORDER BY ts, meter LIMIT 5000",
            (mp, lo, hi)).fetchall()
        conn.execute("COMMIT")
    finally:
        conn.close()
    return [dict(r) for r in rows]


@app.get("/api/meter-changes", summary="换表单查看")
def list_meter_changes(mp: str = Query(None, min_length=1, max_length=64)):
    conn = db.connect()
    try:
        conn.execute("BEGIN")
        if mp:
            rows = conn.execute(
                "SELECT * FROM meter_changes WHERE mp=? ORDER BY change_ts", (mp,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM meter_changes ORDER BY mp, change_ts").fetchall()
        conn.execute("COMMIT")
    finally:
        conn.close()
    return [dict(r) for r in rows]


@app.get("/api/imports", summary="导入批次列表")
def list_imports():
    conn = db.connect()
    try:
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT * FROM imports ORDER BY id DESC LIMIT 100").fetchall()
        conn.execute("COMMIT")
    finally:
        conn.close()
    return [dict(r) for r in rows]


@app.get("/", include_in_schema=False)
def root():
    return {
        "service": "园区能源日用电量核算 API",
        "docs": "/docs",
        "endpoints": ["/api/imports/readings", "/api/imports/meter-changes",
                      "/api/settlement/daily", "/api/settlement/range",
                      "/api/metering-points", "/api/readings", "/api/meter-changes",
                      "/api/imports", "/api/health"],
    }
