"""跨点串表核查核心（不依赖 FastAPI，可单测）。

同一物理表号被归入两个计量点时，两处各自核算都可能给出“确定用量”，
但该表在重叠时段内的用量归属无法确定。本模块依据**相邻整点读数**与
**换表边界读数**形成每块表在每个计量点上的有效计量段，再找出同表号
在不同计量点之间的时段重叠。

有效计量段（segment）：
- 段内每个整点都有该表的有效数据点（常规读数或换表单边界读数）。
- 相邻两个有效整点构成一个占用该表的小时区间；数据点之间必须整点相邻，
  **缺整点即断段，不推定占用**。
- 换表整点 T：旧表的末数据点 / 新表的首数据点由换表单给出，
  旧表段在 T 闭端结束、新表段自 T 开端开始（半开区间 [start, end)）。
- 同一计量点先后使用同一表号、但段与段不重叠（仅端点相接）不报冲突。

来源（sources）：
- kind=reading  来自读数表（含导入批次与来源文件名）
- kind=meter_change  来自换表单边界读数（旧表末读数 / 新表初值）
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from .calc import INSTANCE, _dec, _num, fmt_ts

HOUR = timedelta(hours=1)
HOUR_SECONDS = 3600

REASON_CROSS_METER = "cross_meter_conflict"


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


def _is_next_hour(prev_ts: str, ts: str) -> bool:
    """ts 是否恰好为 prev_ts 的下一个整点（用 datetime 运算，正确处理跨日）。"""
    return _parse(ts) - _parse(prev_ts) == HOUR


def _overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> Optional[tuple[str, str]]:
    """两个半开区间 [a_start,a_end)、[b_start,b_end) 的正长度交集；端点相接不算。"""
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if start < end:
        return start, end
    return None


def _load_points(
    conn,
    mp: Optional[str],
    meters: Optional[set[str]],
    lo: Optional[str],
    hi: Optional[str],
) -> dict[tuple[str, str], dict[str, dict]]:
    """收集 (mp, meter) -> {ts -> {"value", "sources":[...]}} 的有效数据点。

    常规读数与换表单边界读数都视为有效整点数据；同一整点可同时带两类来源。
    """
    points: dict[tuple[str, str], dict[str, dict]] = {}

    def add(mp_: str, meter_: str, ts_: str, value: Decimal, source: dict) -> None:
        bucket = points.setdefault((mp_, meter_), {})
        slot = bucket.get(ts_)
        if slot is None:
            bucket[ts_] = {"value": value, "sources": [source]}
        else:
            slot["sources"].append(source)
            # 数值以已存在的有效读数为准（日核算中的边界冲突由 calc.py 另行标记）
            if slot["value"] is None:
                slot["value"] = value

    meter_list = sorted(meters) if meters is not None else None

    r_where, r_args = ["1=1"], []
    if mp:
        r_where.append("r.mp = ?")
        r_args.append(mp)
    if meter_list is not None:
        r_where.append("r.meter IN (%s)" % ",".join("?" * len(meter_list)))
        r_args.extend(meter_list)
    if lo:
        r_where.append("r.ts >= ?")
        r_args.append(lo)
    if hi:
        r_where.append("r.ts <= ?")
        r_args.append(hi)
    q = (
        "SELECT r.mp, r.meter, r.ts, r.cum_kwh, r.import_id, i.source_name"
        " FROM readings r JOIN imports i ON i.id = r.import_id"
        " WHERE " + " AND ".join(r_where)
    )
    for row in conn.execute(q, r_args):
        add(row["mp"], row["meter"], row["ts"], _dec(row["cum_kwh"]), {
            "kind": "reading",
            "ts": row["ts"],
            "cum_kwh": _num(_dec(row["cum_kwh"])),
            "import_id": row["import_id"],
            "source_name": row["source_name"],
        })

    c_where, c_args = ["1=1"], []
    if mp:
        c_where.append("c.mp = ?")
        c_args.append(mp)
    if meter_list is not None:
        # 独立 AND 过滤：命中表号集合的单据（新旧表号任一命中即可）
        ph = ",".join("?" * len(meter_list))
        c_where.append(f"(c.old_meter IN ({ph}) OR c.new_meter IN ({ph}))")
        c_args.extend(meter_list)
        c_args.extend(meter_list)
    if lo:
        c_where.append("c.change_ts >= ?")
        c_args.append(lo)
    if hi:
        c_where.append("c.change_ts <= ?")
        c_args.append(hi)
    qc = (
        "SELECT c.*, i.source_name AS imp_source FROM meter_changes c"
        " JOIN imports i ON i.id = c.import_id WHERE " + " AND ".join(c_where)
    )
    for row in conn.execute(qc, c_args):
        ts = row["change_ts"]
        base = {
            "ts": ts,
            "change_ts": ts,
            "import_id": row["import_id"],
            "source_name": row["imp_source"],
        }
        if meters is None or row["old_meter"] in meters:
            add(row["mp"], row["old_meter"], ts, _dec(row["old_final_kwh"]), {
                **base, "kind": "meter_change", "role": "old_final",
                "meter": row["old_meter"], "cum_kwh": _num(_dec(row["old_final_kwh"])),
            })
        if meters is None or row["new_meter"] in meters:
            add(row["mp"], row["new_meter"], ts, _dec(row["new_initial_kwh"]), {
                **base, "kind": "meter_change", "role": "new_initial",
                "meter": row["new_meter"], "cum_kwh": _num(_dec(row["new_initial_kwh"])),
            })

    return points


def build_segments(
    conn,
    mp: Optional[str] = None,
    meter: Optional[str] = None,
    lo: Optional[str] = None,
    hi: Optional[str] = None,
    meters: Optional[set[str]] = None,
) -> list[dict]:
    """形成有效计量段。

    可选过滤：mp（计量点）、meter（单个表号）或 meters（表号集合，内部用）、
    lo/hi（整点闭区间时间窗）。时间窗只裁剪数据点，段的起止由数据自身决定。
    """
    if meters is None and meter is not None:
        meters = {meter}
    points = _load_points(conn, mp, meters, lo, hi)
    segments: list[dict] = []

    for (mp_id, meter_id), by_ts in points.items():
        ordered = sorted(by_ts.items())
        run: list[tuple[str, dict]] = []

        def flush(run_points: list[tuple[str, dict]]) -> None:
            if len(run_points) < 2:
                return  # 孤立数据点不构成占用段，不推定
            start_ts = run_points[0][0]
            end_ts = run_points[-1][0]
            sources: list[dict] = []
            seen: set[tuple] = set()
            for _, slot in run_points:
                for src in slot["sources"]:
                    key = (src["kind"], src["ts"], src.get("role"),
                           src["import_id"], src.get("cum_kwh"))
                    if key not in seen:
                        seen.add(key)
                        sources.append(src)
            sources.sort(key=lambda s: (s["ts"], s["kind"], s.get("role") or ""))
            segments.append({
                "mp": mp_id,
                "meter": meter_id,
                "start": start_ts,
                "end": end_ts,  # 半开：实际占用 [start, end)
                "hours": int((_parse(end_ts) - _parse(start_ts)).total_seconds() // HOUR_SECONDS),
                "point_count": len(run_points),
                "sources": sources,
            })

        for item in ordered:
            if run and _is_next_hour(run[-1][0], item[0]):
                run.append(item)
            else:
                flush(run)
                run = [item]
        flush(run)

    segments.sort(key=lambda s: (s["meter"], s["mp"], s["start"], s["end"]))
    return segments


def _source_summary(sources: list[dict]) -> list[dict]:
    """对外返回的来源摘要：读数/单据来自哪些批次与文件。"""
    out: list[dict] = []
    seen: set[tuple] = set()
    for s in sources:
        key = (s["kind"], s["import_id"], s.get("role"))
        if key in seen:
            continue
        seen.add(key)
        item = {
            "kind": s["kind"],
            "import_id": s["import_id"],
            "source_name": s["source_name"],
        }
        if "role" in s:
            item["role"] = s["role"]
        out.append(item)
    return out


def find_cross_meter_conflicts(segments: list[dict]) -> list[dict]:
    """同表号、不同计量点的有效计量段正长度重叠 → 串表冲突。

    同一段集合产生的相邻重叠片合并为一条冲突记录；仅端点相接（半开区间不相交）
    不报冲突，因此同一表号先后在两个点使用、段不重叠时不会命中。
    """
    by_meter: dict[str, list[dict]] = {}
    for seg in segments:
        by_meter.setdefault(seg["meter"], []).append(seg)

    records: list[dict] = []
    for meter_id, segs in by_meter.items():
        segs = sorted(segs, key=lambda s: (s["start"], s["end"], s["mp"]))
        pieces: list[dict] = []
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                a, b = segs[i], segs[j]
                if a["mp"] == b["mp"]:
                    continue
                ov = _overlap(a["start"], a["end"], b["start"], b["end"])
                if ov is None:
                    continue
                involved = tuple(sorted({id(a), id(b)}))
                pieces.append({
                    "start": ov[0],
                    "end": ov[1],
                    "seg_keys": involved,
                    "segments": [a, b],
                })
        if not pieces:
            continue
        pieces.sort(key=lambda p: (p["start"], p["end"]))

        # 合并参与段集合相同且时间相接的重叠片
        merged: list[dict] = []
        for p in pieces:
            if merged and merged[-1]["seg_keys"] == p["seg_keys"] and merged[-1]["end"] == p["start"]:
                merged[-1]["end"] = p["end"]
            else:
                merged.append(dict(p))
        for p in merged:
            involved_segs = sorted(p["segments"], key=lambda s: (s["mp"], s["start"]))
            mps = sorted({s["mp"] for s in involved_segs})
            all_sources: list[dict] = []
            seen: set[tuple] = set()
            for s in involved_segs:
                for src in s["sources"]:
                    key = (s["mp"], src["kind"], src["ts"], src.get("role"),
                           src["import_id"], src.get("cum_kwh"))
                    if key in seen:
                        continue
                    seen.add(key)
                    all_sources.append({
                        "mp": s["mp"],
                        "kind": src["kind"],
                        "ts": src["ts"],
                        "cum_kwh": src.get("cum_kwh"),
                        "import_id": src["import_id"],
                        "source_name": src["source_name"],
                        **({"role": src["role"]} if "role" in src else {}),
                    })
            all_sources.sort(key=lambda s: (s["mp"], s["ts"], s["kind"]))
            records.append({
                "meter": meter_id,
                "start": p["start"],
                "end": p["end"],
                "hours": int((_parse(p["end"]) - _parse(p["start"])).total_seconds() // HOUR_SECONDS),
                "metering_points": mps,
                "segments": [{
                    "mp": s["mp"],
                    "meter": s["meter"],
                    "start": s["start"],
                    "end": s["end"],
                    "sources": _source_summary(s["sources"]),
                } for s in involved_segs],
                "sources": all_sources,
            })

    records.sort(key=lambda r: (r["meter"], r["start"], r["metering_points"]))
    return records


def affected_dates(start_ts: str, end_ts: str) -> list[str]:
    """半开冲突区间 [start,end) 覆盖到的 UTC 结算日。"""
    cur = _parse(start_ts).date()
    last = (_parse(end_ts) - HOUR).date()  # 末端整点所属小时计入前一日期
    days: list[str] = []
    while cur <= last:
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def snapshot_info(conn) -> dict:
    return {
        "max_import_id": conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM imports").fetchone()["m"],
        "instance": INSTANCE,
    }
