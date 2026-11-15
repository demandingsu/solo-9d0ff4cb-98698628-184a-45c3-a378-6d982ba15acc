"""标准库验证脚本（环境无 fastapi/pytest 时使用）：直接驱动真实 SQLite + 核算核心。

运行: python3 -I scripts/_verify_stdlib.py
"""
import csv
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.calc import REASON_CROSS_METER, compute_day  # noqa: E402
from app.segments import (affected_dates, build_segments,  # noqa: E402
                           find_cross_meter_conflicts)

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
failures = []


def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL"), "-", name, extra)
    if not cond:
        failures.append(name)


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def import_readings(conn, path, import_id):
    conn.execute("INSERT INTO imports (id,kind,source_name,row_count) VALUES (?,?,?,?)",
                 (import_id, "readings", os.path.basename(path), 0))
    n = 0
    for r in read_csv(path):
        conn.execute("INSERT INTO readings (mp,meter,ts,cum_kwh,import_id) VALUES (?,?,?,?,?)",
                     (r["mp"], r["meter"], r["ts"], float(r["cum_kwh"]), import_id))
        n += 1
    conn.execute("UPDATE imports SET row_count=? WHERE id=?", (n, import_id))


def import_forms(conn, path, import_id):
    conn.execute("INSERT INTO imports (id,kind,source_name,row_count) VALUES (?,?,?,?)",
                 (import_id, "meter_changes", os.path.basename(path), 0))
    n = 0
    for r in read_csv(path):
        conn.execute("INSERT INTO meter_changes (mp,change_ts,old_meter,new_meter,"
                     "old_final_kwh,new_initial_kwh,import_id) VALUES (?,?,?,?,?,?,?)",
                     (r["mp"], r["change_ts"], r["old_meter"], r["new_meter"],
                      float(r["old_final_kwh"]), float(r["new_initial_kwh"]), import_id))
        n += 1
    conn.execute("UPDATE imports SET row_count=? WHERE id=?", (n, import_id))


def main():
    tmp = tempfile.mkdtemp()
    os.environ["DB_PATH"] = os.path.join(tmp, "verify.db")
    db.init_db()
    conn = db.connect()

    # ---- 1. 导入：MP-001 读数（无换表单）、MP-004 读数+换表单、MP-005 读数 ----
    import_readings(conn, f"{SAMPLES}/readings_mp001.csv", 1)
    import_readings(conn, f"{SAMPLES}/readings_mp004.csv", 2)
    import_forms(conn, f"{SAMPLES}/meter_changes_mp004.csv", 3)
    import_readings(conn, f"{SAMPLES}/readings_mp005.csv", 4)

    conn.execute("BEGIN")
    segs = build_segments(conn)
    conflicts = find_cross_meter_conflicts(segs)
    conn.execute("COMMIT")

    print("\n== segments ==")
    for s in segs:
        print(f"  {s['mp']:7} {s['meter']:5} {s['start']} -> {s['end']} ({s['hours']}h)")
    print("== conflicts ==")
    for c in conflicts:
        print(f"  {c['meter']} {c['start']} -> {c['end']} {c['metering_points']}")

    # 2. 只有一条冲突：M100 08:00-14:00，MP-001 vs MP-004
    check("仅一条跨点冲突", len(conflicts) == 1)
    c = conflicts[0]
    check("冲突表号 M100", c["meter"] == "M100")
    check("冲突时段 08:00-14:00 共6小时",
          (c["start"], c["end"], c["hours"]) ==
          ("2026-01-09T08:00:00Z", "2026-01-09T14:00:00Z", 6))
    check("涉及计量点 MP-001/MP-004", c["metering_points"] == ["MP-001", "MP-004"])
    check("affected_dates 仅 2026-01-09",
          affected_dates(c["start"], c["end"]) == ["2026-01-09"])
    kinds = {(s["mp"], s["kind"], s.get("role")) for s in c["sources"]}
    check("来源含读数与换表单(old_final)",
          ("MP-004", "meter_change", "old_final") in kinds
          and ("MP-001", "reading", None) in kinds)
    check("来源带 import_id/source_name",
          all("import_id" in s and "source_name" in s for s in c["sources"]))

    # 3. MP-005：同表号先后使用仅端点相接 -> 无冲突；缺 14:00 -> 断成两段
    mp005 = [s for s in segs if s["mp"] == "MP-005"]
    check("MP-005 缺整点断成两段", len(mp005) == 2
          and [(s["start"], s["end"]) for s in mp005] ==
          [("2026-01-10T00:00:00Z", "2026-01-10T13:00:00Z"),
           ("2026-01-10T15:00:00Z", "2026-01-11T00:00:00Z")])
    check("MP-005 段不参与任何冲突",
          all("MP-005" not in x["metering_points"] for x in conflicts))

    # 4. MP-004 的 M100 段端点 14:00 来自换表单；M600 段自 14:00 开始
    mp004_m100 = next(s for s in segs if s["mp"] == "MP-004" and s["meter"] == "M100")
    check("MP-004 M100 段 08:00-14:00",
          (mp004_m100["start"], mp004_m100["end"]) ==
          ("2026-01-09T08:00:00Z", "2026-01-09T14:00:00Z"))
    check("MP-004 M100 段末点来源含换表单",
          any(src["kind"] == "meter_change" and src["role"] == "old_final"
              for src in mp004_m100["sources"]))
    mp004_m600 = next(s for s in segs if s["mp"] == "MP-004" and s["meter"] == "M600")
    check("MP-004 M600 段自换表整点 14:00 起",
          mp004_m600["start"] == "2026-01-09T14:00:00Z"
          and any(src["kind"] == "meter_change" and src["role"] == "new_initial"
                  for src in mp004_m600["sources"]))

    # 5. 日核算 MP-001 2026-01-09（补录换表单前）：跨点冲突 + 无单据换表 -> 待核
    d = compute_day(conn, "MP-001", date(2026, 1, 9))
    check("MP-001 0109 状态 pending", d["status"] == "pending")
    check("MP-001 0109 无确定日总量", d["total_kwh"] is None)
    ivs = [(i["start"], i["end"], tuple(i["reasons"])) for i in d["pending_intervals"]]
    print("  MP-001 0109 intervals:", ivs)
    check("MP-001 0109 待核区间=跨点(08-14)+无单据换表(23-24)",
          ("2026-01-09T08:00:00Z", "2026-01-09T14:00:00Z",
           ("cross_meter_conflict",)) in ivs
          and any(r == "unrecorded_meter_change" for _, _, rs in ivs for r in rs))
    check("MP-001 0109 响应含结构化跨点冲突", len(d["cross_meter_conflicts"]) == 1
          and d["cross_meter_conflicts"][0]["meter"] == "M100")
    cross_inc = [i for i in d["increments"]
                 if "2026-01-09T08:00:00Z" <= i["start"] < "2026-01-09T14:00:00Z"]
    check("冲突 6 个增量全部 pending 且带原因",
          len(cross_inc) == 6 and all(
              i["status"] == "pending" and REASON_CROSS_METER in i["reasons"]
              and i["delta_kwh"] is None for i in cross_inc))
    check("快照水位 max_import_id=4", d["snapshot"]["max_import_id"] == 4)

    # 6. MP-004 自身日核算：重叠小时待核，其余确定
    d4 = compute_day(conn, "MP-004", date(2026, 1, 9))
    check("MP-004 0109 pending 且无总量", d4["status"] == "pending" and d4["total_kwh"] is None)
    check("MP-004 0109 跨点冲突记录存在",
          len(d4["cross_meter_conflicts"]) == 1
          and d4["cross_meter_conflicts"][0]["metering_points"] == ["MP-001", "MP-004"])

    # 7. MP-005 日核算：缺整点待核，但绝无跨点原因
    d5 = compute_day(conn, "MP-005", date(2026, 1, 10))
    check("MP-005 0110 无跨点冲突", d5["cross_meter_conflicts"] == []
          and all(REASON_CROSS_METER not in i["reasons"] for i in d5["increments"]))
    check("MP-005 0110 缺整点待核", d5["status"] == "pending"
          and any("missing_hour" in iv["reasons"] for iv in d5["pending_intervals"]))

    # 8. 补录 MP-001 迟到换表单 -> 按当前数据重判：无单据换表消失，跨点冲突仍在
    import_forms(conn, f"{SAMPLES}/meter_changes_mp001_late.csv", 5)
    d2 = compute_day(conn, "MP-001", date(2026, 1, 9))
    reasons_flat = [r for iv in d2["pending_intervals"] for r in iv["reasons"]]
    check("补录后无单据换表原因消失", "unrecorded_meter_change" not in reasons_flat)
    check("补录后跨点冲突仍待核、无确定总量",
          d2["status"] == "pending" and d2["total_kwh"] is None
          and REASON_CROSS_METER in reasons_flat)
    check("补录后 known_kwh=180（240 扣除 6 个重叠小时各 10）", d2["known_kwh"] == 180.0)
    check("补录后快照水位=5", d2["snapshot"]["max_import_id"] == 5)
    # 次日不再受跨点影响（M100 段止于午夜），MP-001 0110 应确定 120
    d3 = compute_day(conn, "MP-001", date(2026, 1, 10))
    check("MP-001 0110 确定 120（跨点冲突不跨日）",
          d3["status"] == "ok" and d3["total_kwh"] == 120.0
          and d3["cross_meter_conflicts"] == [])

    # 9. 失败导入回滚不改变核查结果
    before = find_cross_meter_conflicts(build_segments(conn))
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO imports (id,kind,source_name,row_count) VALUES (6,'readings','bad',2)")
        # 合法新行（回滚后不应残留）
        conn.execute("INSERT INTO readings (mp,meter,ts,cum_kwh,import_id) VALUES (?,?,?,?,?)",
                     ("MP-001", "M100", "2026-01-12T00:00:00Z", 2000.0, 6))
        # 与已提交读数冲突 -> 模拟整批校验失败
        conn.execute("INSERT INTO readings (mp,meter,ts,cum_kwh,import_id) VALUES (?,?,?,?,?)",
                     ("MP-001", "M100", "2026-01-09T05:00:00Z", 9999.0, 6))
        raise RuntimeError("simulated validation failure")
    except (RuntimeError, sqlite3.IntegrityError):
        conn.execute("ROLLBACK")
    after = find_cross_meter_conflicts(build_segments(conn))
    check("失败导入回滚后冲突结果不变",
          [(c["meter"], c["start"], c["end"]) for c in before] ==
          [(c["meter"], c["start"], c["end"]) for c in after])
    check("回滚无痕迹（imports 仍 5 批）",
          conn.execute("SELECT COUNT(*) c FROM imports").fetchone()["c"] == 5)

    # 10. 过滤接口：按 mp / meter / 时间窗
    conn.execute("BEGIN")
    win = build_segments(conn, lo="2026-01-09T00:00:00Z", hi="2026-01-09T23:00:00Z",
                         meters={"M100"})
    cc = find_cross_meter_conflicts(win)
    conn.execute("COMMIT")
    check("时间窗+表号过滤仍检出冲突", len(cc) == 1 and cc[0]["meter"] == "M100")
    only_mp005 = find_cross_meter_conflicts(build_segments(conn, mp="MP-005"))
    check("按 MP-005 过滤无冲突", only_mp005 == [])

    # 11. 人工构造：同表号两段仅端点相接（不同点）-> 不报；改成重叠 1 小时 -> 报
    t0 = datetime(2026, 2, 1, tzinfo=timezone.utc)
    def h(i):
        return (t0 + i * timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO imports (id,kind,source_name,row_count) VALUES (7,'readings','syn',10)")
    for i in range(4):  # MP-X M777: 00:00-03:00（含 03 点，段端到 03:00）
        conn.execute("INSERT INTO readings (mp,meter,ts,cum_kwh,import_id) VALUES (?,?,?,?,?)",
                     ("MP-X", "M777", h(i), float(100 + i), 7))
    for i in range(3, 6):  # MP-Y M777: 03:00-06:00，起初与 MP-X 仅端点相接
        conn.execute("INSERT INTO readings (mp,meter,ts,cum_kwh,import_id) VALUES (?,?,?,?,?)",
                     ("MP-Y", "M777", h(i), float(200 + i), 7))
    touch = find_cross_meter_conflicts(build_segments(conn, meter="M777"))
    check("同表号两段仅端点相接不报冲突", touch == [])
    conn.execute("INSERT INTO readings (mp,meter,ts,cum_kwh,import_id) VALUES (?,?,?,?,?)",
                 ("MP-Y", "M777", h(2), 199.0, 7))  # 02:00 补点 -> MP-Y 段前延，重叠 1h
    ov = find_cross_meter_conflicts(build_segments(conn, meter="M777"))
    check("补点后重叠 1 小时报冲突", len(ov) == 1
          and (ov[0]["start"], ov[0]["end"], ov[0]["hours"]) == (h(2), h(3), 1))

    conn.close()
    print("\n%s" % ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
