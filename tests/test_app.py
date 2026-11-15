"""端到端与并发一致性测试。每个用例使用独立临时 SQLite 库。"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.parsing import ReadingRow

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    with TestClient(main.app) as c:
        yield c


def post_csv(client, url, content: str, name="data.csv"):
    return client.post(url, files={"file": (name, content.encode(), "text/csv")})


def hourly(mp, meter, start, values):
    t0 = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    lines = ["mp,meter,ts,cum_kwh"]
    for i, v in enumerate(values):
        ts = (t0 + i * timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"{mp},{meter},{ts},{v}")
    return "\n".join(lines) + "\n"


# MP-001 样例：跨午夜换表
MP001_OLD = hourly("MP-001", "M100", "2026-01-09T00:00:00Z",
                   [1000 + 10 * i for i in range(24)])          # 00..23
MP001_NEW = hourly("MP-001", "M200", "2026-01-10T00:00:00Z",
                   [40 + 5 * i for i in range(25)])            # 午夜起 25 个整点
MP001_FORM = ("mp,change_ts,old_meter,new_meter,old_final_kwh,new_initial_kwh\n"
              "MP-001,2026-01-10T00:00:00Z,M100,M200,1240.0,40.0\n")


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_simple_day_ok(client):
    post_csv(client, "/api/imports/readings",
             hourly("MP-A", "M1", "2026-02-01T00:00:00Z", [100 + 2 * i for i in range(25)]))
    r = client.get("/api/settlement/daily", params={"mp": "MP-A", "date": "2026-02-01"})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "ok"
    assert body["total_kwh"] == 48.0
    assert body["pending_intervals"] == []
    assert len(body["increments"]) == 24


def test_cross_midnight_change_then_late_form(client):
    # 1) 只导入读数：午夜换表无单据 -> 待核
    assert post_csv(client, "/api/imports/readings", MP001_OLD + MP001_NEW.split("\n", 1)[1]).status_code == 200
    r = client.get("/api/settlement/daily", params={"mp": "MP-001", "date": "2026-01-09"}).json()
    assert r["status"] == "pending"
    assert r["total_kwh"] is None
    assert r["known_kwh"] == 230.0
    assert r["pending_intervals"] == [{
        "start": "2026-01-09T23:00:00Z", "end": "2026-01-10T00:00:00Z",
        "reasons": ["unrecorded_meter_change"],
        "details": ["表计由 M100 切换为 M200，但无换表单"],
    }]

    # 2) 迟到换表单补录 -> 返回受影响日期
    r = post_csv(client, "/api/imports/meter-changes", MP001_FORM, "late.csv")
    assert r.status_code == 200
    assert r.json()["affected_dates"] == {"MP-001": ["2026-01-09", "2026-01-10"]}

    # 3) 重算：跨午夜两天都确定
    r9 = client.get("/api/settlement/daily", params={"mp": "MP-001", "date": "2026-01-09"}).json()
    assert r9["status"] == "ok" and r9["total_kwh"] == 240.0
    r10 = client.get("/api/settlement/daily", params={"mp": "MP-001", "date": "2026-01-10"}).json()
    assert r10["status"] == "ok" and r10["total_kwh"] == 120.0


def test_missing_hour_and_regression(client):
    vals = {0: 500, 1: 508, 2: 516, 3: 524, 4: 532, 5: 540, 6: 535, 7: 543,
            8: 551, 9: 559, 10: 567, 11: 575, 12: 583, 13: 591, 15: 607,
            16: 615, 17: 623, 18: 631, 19: 639, 20: 647, 21: 655, 22: 663,
            23: 671, 24: 679}
    t0 = datetime(2026, 1, 9, tzinfo=timezone.utc)
    lines = ["mp,meter,ts,cum_kwh"] + [
        f"MP-002,M300,{(t0 + h * timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')},{v}.0"
        for h, v in sorted(vals.items())]
    assert post_csv(client, "/api/imports/readings", "\n".join(lines)).status_code == 200

    r = client.get("/api/settlement/daily", params={"mp": "MP-002", "date": "2026-01-09"}).json()
    assert r["status"] == "pending" and r["total_kwh"] is None
    assert r["known_kwh"] == 168.0
    intervals = [(i["start"], i["end"], i["reasons"]) for i in r["pending_intervals"]]
    assert intervals == [
        ("2026-01-09T05:00:00Z", "2026-01-09T06:00:00Z", ["meter_regression"]),
        ("2026-01-09T13:00:00Z", "2026-01-09T15:00:00Z", ["missing_hour"]),
    ]


def test_boundary_conflict(client):
    old = hourly("MP-003", "M400", "2026-01-09T00:00:00Z", [2000 + 20 * i for i in range(13)])
    new = hourly("MP-003", "M500", "2026-01-09T12:00:00Z", [105 + 15 * i for i in range(13)])
    assert post_csv(client, "/api/imports/readings", old + new.split("\n", 1)[1]).status_code == 200
    form = ("mp,change_ts,old_meter,new_meter,old_final_kwh,new_initial_kwh\n"
            "MP-003,2026-01-09T12:00:00Z,M400,M500,2240.0,100.0\n")
    assert post_csv(client, "/api/imports/meter-changes", form).status_code == 200

    r = client.get("/api/settlement/daily", params={"mp": "MP-003", "date": "2026-01-09"}).json()
    assert r["status"] == "pending" and r["total_kwh"] is None
    assert r["known_kwh"] == 385.0
    assert len(r["pending_intervals"]) == 1
    iv = r["pending_intervals"][0]
    assert iv["start"] == "2026-01-09T11:00:00Z" and iv["end"] == "2026-01-09T13:00:00Z"
    assert iv["reasons"] == ["boundary_conflict"]


def test_failed_import_rolls_back(client):
    assert post_csv(client, "/api/imports/readings", MP001_OLD).status_code == 200
    before = client.get("/api/imports").json()

    bad = ("mp,meter,ts,cum_kwh\n"
           "MP-001,M100,2026-01-12T00:00:00Z,2000.0\n"   # 合法新行，不应入库
           "MP-001,M100,2026-01-09T05:00:00Z,9999.0\n")  # 与已有 1050 冲突
    r = post_csv(client, "/api/imports/readings", bad)
    assert r.status_code == 409

    # 已有结果不变：imports 不变、合法新行未入库、核算不变
    assert client.get("/api/imports").json() == before
    assert client.get("/api/readings",
                      params={"mp": "MP-001", "from": "2026-01-12", "to": "2026-01-12"}).json() == []
    r9 = client.get("/api/settlement/daily", params={"mp": "MP-001", "date": "2026-01-09"}).json()
    assert r9["status"] == "pending" and r9["known_kwh"] == 230.0


def test_duplicate_import_idempotent(client):
    assert post_csv(client, "/api/imports/readings", MP001_OLD).status_code == 200
    r = post_csv(client, "/api/imports/readings", MP001_OLD)
    assert r.status_code == 200
    assert r.json()["inserted"] == 0 and r.json()["duplicates_skipped"] == 24


def test_validation_error_nothing_stored(client):
    bad = "mp,meter,ts,cum_kwh\nMP-X,M1,2026-01-09T00:30:00Z,1.0\n"  # 非整点
    r = post_csv(client, "/api/imports/readings", bad)
    assert r.status_code == 422
    assert client.get("/api/imports").json() == []


def test_range_endpoint(client):
    post_csv(client, "/api/imports/readings",
             hourly("MP-R", "M1", "2026-03-01T00:00:00Z", [10 * i for i in range(49)]))
    r = client.get("/api/settlement/range",
                   params={"mp": "MP-R", "from": "2026-03-01", "to": "2026-03-02"})
    days = r.json()["days"]
    assert [d["total_kwh"] for d in days] == [240.0, 240.0]
    assert r.status_code == 200


def test_concurrent_imports_and_snapshot_reads(tmp_path, monkeypatch):
    """多线程并发导入（含失败批）+ 只读事务：读到的每个批次要么全有要么全无。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "conc.db"))
    db.init_db()
    errors: list[str] = []
    t0 = datetime(2026, 1, 9, tzinfo=timezone.utc)

    def rows_for(batch: int, n: int = 5):
        out = []
        for k in range(n):
            i = batch * n + k
            ts = (t0 + i * timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            out.append(ReadingRow("MP-C", "M9", ts, Decimal(1000 + 10 * i)))
        return out

    # 先提交一批，失败批与之冲突，避免与并发批次产生不确定竞态
    main._store_readings(rows_for(0), "seed")

    def importer(base: int):
        try:
            for b in range(base, base + 10):
                main._store_readings(rows_for(b), f"src-{b}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"importer{base}: {e}")

    def failing_importer():
        # 与已提交读数冲突 -> 整批回滚，不留痕迹
        conflict = [ReadingRow("MP-C", "M9",
                               t0.strftime("%Y-%m-%dT%H:%M:%SZ"), Decimal("99999"))]
        try:
            main._store_readings(conflict, "bad")
        except main.HTTPException as e:
            assert e.status_code == 409
        else:
            errors.append("failing importer unexpectedly succeeded")

    def reader():
        try:
            for _ in range(120):
                conn = db.connect()
                try:
                    conn.execute("BEGIN")  # 只读事务 = 同一已提交快照
                    batches = conn.execute(
                        "SELECT id, row_count FROM imports WHERE kind='readings'").fetchall()
                    for b in batches:
                        n = conn.execute(
                            "SELECT COUNT(*) AS c FROM readings WHERE import_id=?",
                            (b["id"],)).fetchone()["c"]
                        if n != b["row_count"]:
                            errors.append(f"partial import visible: {dict(b)} has {n} rows")
                    # 快照内自洽：max_import_id 与可见批次一致
                    maxid = conn.execute(
                        "SELECT COALESCE(MAX(id),0) AS m FROM imports").fetchone()["m"]
                    if batches and max(b["id"] for b in batches) != maxid:
                        errors.append("snapshot inconsistent")
                    conn.execute("COMMIT")
                finally:
                    conn.close()
        except Exception as e:  # noqa: BLE001
            errors.append(f"reader: {e}")

    threads = ([threading.Thread(target=importer, args=(b,)) for b in (10, 20, 30, 40)]
               + [threading.Thread(target=failing_importer)]
               + [threading.Thread(target=reader) for _ in range(2)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    conn = db.connect()
    try:
        # 41 批 * 5 行全部提交；失败批无痕迹
        assert conn.execute("SELECT COUNT(*) AS c FROM readings").fetchone()["c"] == 205
        assert conn.execute("SELECT COUNT(*) AS c FROM imports").fetchone()["c"] == 41
    finally:
        conn.close()


# ---------------------------------------------------------------- 跨点串表核查

def readings_between(mp, meter, start, end_exclusive, value_at):
    """生成 [start, end_exclusive) 各整点的读数 CSV；value_at(i, ts) 给累计值。"""
    t0 = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    t1 = datetime.strptime(end_exclusive, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    n = int((t1 - t0).total_seconds() // 3600)
    lines = ["mp,meter,ts,cum_kwh"]
    for i in range(n + 1):  # 含末端整点
        ts = (t0 + i * timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"{mp},{meter},{ts},{value_at(i)}")
    return "\n".join(lines) + "\n"


def test_cross_meter_overlap_marks_both_points_pending(client):
    # MP-A 的 M9 覆盖 00:00-24:00；MP-B 的 M9 覆盖 08:00-14:00 -> 重叠 6 小时
    post_csv(client, "/api/imports/readings",
             readings_between("MP-A", "M9", "2026-01-09T00:00:00Z", "2026-01-10T00:00:00Z",
                              lambda i: 1000 + 10 * i))
    post_csv(client, "/api/imports/readings",
             readings_between("MP-B", "M9", "2026-01-09T08:00:00Z", "2026-01-09T14:00:00Z",
                              lambda i: 5000 + 7 * i))

    r = client.get("/api/meter-cross-checks").json()
    assert r["status"] == "conflict" and r["conflict_count"] == 1
    c = r["conflicts"][0]
    assert c["meter"] == "M9"
    assert (c["start"], c["end"], c["hours"]) == (
        "2026-01-09T08:00:00Z", "2026-01-09T14:00:00Z", 6)
    assert c["metering_points"] == ["MP-A", "MP-B"]
    assert c["affected_dates"] == ["2026-01-09"]
    assert all("import_id" in s and "source_name" in s for s in c["sources"])
    assert r["snapshot"]["max_import_id"] >= 2

    # 两个计量点的日核算都必须待核、无确定日总量
    for mp in ("MP-A", "MP-B"):
        d = client.get("/api/settlement/daily", params={"mp": mp, "date": "2026-01-09"}).json()
        assert d["status"] == "pending" and d["total_kwh"] is None
        assert len(d["cross_meter_conflicts"]) == 1
        iv = next(i for i in d["pending_intervals"]
                  if "cross_meter_conflict" in i["reasons"])
        assert (iv["start"], iv["end"]) == (
            "2026-01-09T08:00:00Z", "2026-01-09T14:00:00Z")

    # MP-A 的 6 个重叠增量不给确定用量，其余 18 个仍确定（known_kwh=180）
    da = client.get("/api/settlement/daily",
                    params={"mp": "MP-A", "date": "2026-01-09"}).json()
    assert da["known_kwh"] == 180.0
    cross = [i for i in da["increments"] if "cross_meter_conflict" in i["reasons"]]
    assert len(cross) == 6 and all(i["delta_kwh"] is None and i["status"] == "pending"
                                   for i in cross)


def test_sequential_reuse_no_overlap_no_conflict(client):
    # MP-A M9 段止于 10T00:00；MP-B M9 段自 10T00:00 起：仅端点相接 -> 不报
    post_csv(client, "/api/imports/readings",
             readings_between("MP-A", "M9", "2026-01-09T00:00:00Z", "2026-01-10T00:00:00Z",
                              lambda i: 1000 + 10 * i))
    post_csv(client, "/api/imports/readings",
             readings_between("MP-B", "M9", "2026-01-10T00:00:00Z", "2026-01-11T00:00:00Z",
                              lambda i: 300 + 3 * i))
    r = client.get("/api/meter-cross-checks").json()
    assert r["status"] == "ok" and r["conflicts"] == []
    seg = client.get("/api/meter-segments", params={"meter": "M9"}).json()["segments"]
    assert {s["mp"] for s in seg} == {"MP-A", "MP-B"}


def test_missing_hour_splits_segment_no_occupation_inference(client):
    # MP-B 的 M9 在 14:00 缺整点：断成两段，缺小时不推定占用、不产生跨点冲突
    csv_text = readings_between("MP-B", "M9", "2026-01-10T00:00:00Z",
                                "2026-01-11T00:00:00Z", lambda i: 300 + 3 * i)
    lines = [ln for ln in csv_text.splitlines()
             if "2026-01-10T14:00:00Z" not in ln]
    post_csv(client, "/api/imports/readings", "\n".join(lines) + "\n")
    post_csv(client, "/api/imports/readings",
             readings_between("MP-A", "M9", "2026-01-09T00:00:00Z", "2026-01-10T00:00:00Z",
                              lambda i: 1000 + 10 * i))
    r = client.get("/api/meter-cross-checks").json()
    assert r["conflicts"] == []
    segs = client.get("/api/meter-segments", params={"mp": "MP-B"}).json()["segments"]
    assert [(s["start"], s["end"]) for s in segs] == [
        ("2026-01-10T00:00:00Z", "2026-01-10T13:00:00Z"),
        ("2026-01-10T15:00:00Z", "2026-01-11T00:00:00Z"),
    ]


def test_meter_change_boundary_is_segment_source(client):
    # MP-B：14:00 换表单 M9->M8；M9 段端点由旧表末读数（单据）给出
    post_csv(client, "/api/imports/readings",
             readings_between("MP-A", "M9", "2026-01-09T00:00:00Z", "2026-01-10T00:00:00Z",
                              lambda i: 1000 + 10 * i))
    post_csv(client, "/api/imports/readings",
             readings_between("MP-B", "M9", "2026-01-09T08:00:00Z", "2026-01-09T13:00:00Z",
                              lambda i: 5000 + 7 * i))
    form = ("mp,change_ts,old_meter,new_meter,old_final_kwh,new_initial_kwh\n"
            "MP-B,2026-01-09T14:00:00Z,M9,M8,5042.0,20.0\n")
    post_csv(client, "/api/imports/meter-changes", form)

    segs = client.get("/api/meter-segments",
                      params={"mp": "MP-B", "meter": "M9"}).json()["segments"]
    assert len(segs) == 1 and segs[0]["end"] == "2026-01-09T14:00:00Z"
    assert any(s["kind"] == "meter_change" and s.get("role") == "old_final"
               for s in segs[0]["sources"])

    r = client.get("/api/meter-cross-checks", params={"meter": "M9"}).json()
    assert r["conflict_count"] == 1
    src_kinds = {(s["mp"], s["kind"], s.get("role")) for s in r["conflicts"][0]["sources"]}
    assert ("MP-B", "meter_change", "old_final") in src_kinds


def test_supplementary_reading_rejudges_conflict(client):
    # 初始仅端点相接无冲突；补录一个整点后产生重叠 -> 重新判定即待核；再核算无确定总量
    post_csv(client, "/api/imports/readings",
             readings_between("MP-A", "M9", "2026-01-09T00:00:00Z", "2026-01-09T03:00:00Z",
                              lambda i: 100 + i))
    post_csv(client, "/api/imports/readings",
             readings_between("MP-B", "M9", "2026-01-09T03:00:00Z", "2026-01-09T06:00:00Z",
                              lambda i: 200 + i))
    assert client.get("/api/meter-cross-checks").json()["conflicts"] == []

    post_csv(client, "/api/imports/readings",
             "mp,meter,ts,cum_kwh\nMP-B,M9,2026-01-09T02:00:00Z,199.0\n")
    r = client.get("/api/meter-cross-checks").json()
    assert r["conflict_count"] == 1
    assert (r["conflicts"][0]["start"], r["conflicts"][0]["end"]) == (
        "2026-01-09T02:00:00Z", "2026-01-09T03:00:00Z")
    d = client.get("/api/settlement/daily",
                   params={"mp": "MP-A", "date": "2026-01-09"}).json()
    assert d["status"] == "pending" and d["total_kwh"] is None


def test_cross_check_filters(client):
    post_csv(client, "/api/imports/readings",
             readings_between("MP-A", "M9", "2026-01-09T00:00:00Z", "2026-01-10T00:00:00Z",
                              lambda i: 1000 + 10 * i))
    post_csv(client, "/api/imports/readings",
             readings_between("MP-B", "M9", "2026-01-09T08:00:00Z", "2026-01-09T14:00:00Z",
                              lambda i: 5000 + 7 * i))
    # 按不相关计量点过滤
    assert client.get("/api/meter-cross-checks",
                      params={"mp": "MP-Z"}).json()["conflicts"] == []
    # 按不相关表号过滤
    assert client.get("/api/meter-cross-checks",
                      params={"meter": "NOPE"}).json()["conflicts"] == []
    # 时间窗覆盖冲突时段
    hit = client.get("/api/meter-cross-checks",
                     params={"from": "2026-01-09", "to": "2026-01-09"}).json()
    assert hit["conflict_count"] == 1
    # 时间窗不覆盖
    miss = client.get("/api/meter-cross-checks",
                      params={"from": "2026-01-10", "to": "2026-01-11"}).json()
    assert miss["conflicts"] == []


def test_failed_import_does_not_change_cross_check(client):
    post_csv(client, "/api/imports/readings",
             readings_between("MP-A", "M9", "2026-01-09T00:00:00Z", "2026-01-10T00:00:00Z",
                              lambda i: 1000 + 10 * i))
    before = client.get("/api/meter-cross-checks").json()
    bad = ("mp,meter,ts,cum_kwh\n"
           "MP-B,M9,2026-01-09T10:00:00Z,1.0\n"          # 合法新行（应随批回滚）
           "MP-A,M9,2026-01-09T05:00:00Z,9999.0\n")      # 与已提交读数冲突
    assert post_csv(client, "/api/imports/readings", bad).status_code == 409
    after = client.get("/api/meter-cross-checks").json()
    assert before["conflicts"] == after["conflicts"] == []
    assert client.get("/api/meter-segments", params={"mp": "MP-B"}).json()["segments"] == []


def test_cross_check_snapshot_under_concurrency(tmp_path, monkeypatch):
    """并发导入 + 跨点核查：单次核查在同一只读快照内完成，结果自洽。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "snap.db"))
    db.init_db()
    errors: list[str] = []

    def csv_for(mp, meter, n):
        t0 = datetime(2026, 1, 9, tzinfo=timezone.utc)
        rows = [ReadingRow(mp, meter,
                           (t0 + k * timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           Decimal(100 + k)) for k in range(n)]
        return rows

    main._store_readings(csv_for("MP-A", "M9", 25), "a-seed")
    main._store_readings(csv_for("MP-B", "M7", 25), "b-seed")

    stop = threading.Event()

    def importer():
        b = 0
        while not stop.is_set():
            try:
                main._store_readings(csv_for(f"MP-I{b % 4}", "M9", 25), f"src-{b}")
            except main.HTTPException as e:
                if e.status_code != 409:
                    errors.append(f"importer: {e}")
            b += 1

    def checker():
        for _ in range(100):
            conn = db.connect()
            try:
                conn.execute("BEGIN")  # 只读事务 = 同一已提交快照
                from app.segments import build_segments, find_cross_meter_conflicts
                segs = build_segments(conn)
                conflicts = find_cross_meter_conflicts(segs)
                maxid = conn.execute(
                    "SELECT COALESCE(MAX(id),0) AS m FROM imports").fetchone()["m"]
                # 快照自洽：核查用到的每个来源批次都必须是已提交批次
                visible = {r["id"] for r in conn.execute("SELECT id FROM imports")}
                for c in conflicts:
                    for s in c["sources"]:
                        if s["import_id"] not in visible:
                            errors.append("conflict references uncommitted import")
                if maxid not in visible:
                    errors.append("snapshot watermark inconsistent")
                conn.execute("COMMIT")
            finally:
                conn.close()

    threads = [threading.Thread(target=importer) for _ in range(3)] + \
              [threading.Thread(target=checker) for _ in range(2)]
    for t in threads:
        t.start()
    threading.Event().wait(2.0)
    stop.set()
    for t in threads:
        t.join()
    assert errors == []
