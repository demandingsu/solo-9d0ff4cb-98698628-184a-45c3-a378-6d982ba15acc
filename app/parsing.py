"""CSV 解析与校验（不依赖 FastAPI，可单测）。任何校验错误都使整批导入失败。"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .calc import fmt_ts

MAX_ROWS = 200_000
MAX_BYTES = 10 * 1024 * 1024
MAX_ERRORS = 20

READINGS_FIELDS = ["mp", "meter", "ts", "cum_kwh"]
METER_CHANGES_FIELDS = ["mp", "change_ts", "old_meter", "new_meter",
                        "old_final_kwh", "new_initial_kwh"]


class CsvError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class ReadingRow:
    mp: str
    meter: str
    ts: str          # 归一化 YYYY-MM-DDTHH:00:00Z
    value: Decimal


@dataclass(frozen=True)
class ChangeRow:
    mp: str
    change_ts: str   # 归一化 YYYY-MM-DDTHH:00:00Z
    old_meter: str
    new_meter: str
    old_final: Decimal
    new_initial: Decimal


def _parse_hour(raw: str, errors: list[str], rowno: int, field: str):
    s = (raw or "").strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        errors.append(f"第 {rowno} 行: {field}={s!r} 不是合法 ISO 时间")
        return None
    if dt.tzinfo is None:
        errors.append(f"第 {rowno} 行: {field}={s!r} 必须带 UTC 时区（如 2026-01-10T00:00:00Z）")
        return None
    dt = dt.astimezone(timezone.utc)
    if dt.minute or dt.second or dt.microsecond:
        errors.append(f"第 {rowno} 行: {field}={s!r} 不是整点")
        return None
    return fmt_ts(dt)


def _parse_kwh(raw: str, errors: list[str], rowno: int, field: str):
    s = (raw or "").strip()
    try:
        v = Decimal(s)
    except (InvalidOperation, ValueError):
        errors.append(f"第 {rowno} 行: {field}={s!r} 不是数字")
        return None
    if not v.is_finite() or v < 0 or v > Decimal("1e12"):
        errors.append(f"第 {rowno} 行: {field}={s!r} 超出合法范围 [0, 1e12]")
        return None
    return v


def _parse_name(raw: str, errors: list[str], rowno: int, field: str):
    s = (raw or "").strip()
    if not s or len(s) > 64 or any(ord(c) < 32 for c in s):
        errors.append(f"第 {rowno} 行: {field} 为空或含非法字符")
        return None
    return s


def _read_rows(data: bytes, required: list[str]) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    if len(data) > MAX_BYTES:
        return [], [f"文件超过大小限制 {MAX_BYTES} 字节"]
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], ["文件不是合法 UTF-8 编码"]
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing_cols = [c for c in required if c not in header]
    if missing_cols:
        return [], [f"缺少必需列: {', '.join(missing_cols)}；表头为: {', '.join(header) or '(空)'}"]
    rows = list(reader)
    if len(rows) > MAX_ROWS:
        return [], [f"行数超过限制 {MAX_ROWS}"]
    if not rows:
        errors.append("CSV 没有数据行")
    return rows, errors


def parse_readings_csv(data: bytes) -> list[ReadingRow]:
    rows, errors = _read_rows(data, READINGS_FIELDS)
    out: list[ReadingRow] = []
    seen: dict[tuple[str, str, str], Decimal] = {}
    for idx, raw in enumerate(rows, start=2):  # 第 1 行是表头
        if len(errors) >= MAX_ERRORS:
            break
        mp = _parse_name(raw.get("mp", ""), errors, idx, "mp")
        meter = _parse_name(raw.get("meter", ""), errors, idx, "meter")
        ts = _parse_hour(raw.get("ts", ""), errors, idx, "ts")
        value = _parse_kwh(raw.get("cum_kwh", ""), errors, idx, "cum_kwh")
        if None in (mp, meter, ts, value):
            continue
        key = (mp, meter, ts)
        if key in seen:
            if seen[key] != value:
                errors.append(f"第 {idx} 行: 文件内重复读数 {key} 且数值不一致")
            continue
        seen[key] = value
        out.append(ReadingRow(mp, meter, ts, value))
    if errors:
        raise CsvError(errors[:MAX_ERRORS])
    return out


def parse_meter_changes_csv(data: bytes) -> list[ChangeRow]:
    rows, errors = _read_rows(data, METER_CHANGES_FIELDS)
    out: list[ChangeRow] = []
    seen: dict[tuple[str, str], ChangeRow] = {}
    for idx, raw in enumerate(rows, start=2):
        if len(errors) >= MAX_ERRORS:
            break
        mp = _parse_name(raw.get("mp", ""), errors, idx, "mp")
        change_ts = _parse_hour(raw.get("change_ts", ""), errors, idx, "change_ts")
        old_meter = _parse_name(raw.get("old_meter", ""), errors, idx, "old_meter")
        new_meter = _parse_name(raw.get("new_meter", ""), errors, idx, "new_meter")
        old_final = _parse_kwh(raw.get("old_final_kwh", ""), errors, idx, "old_final_kwh")
        new_initial = _parse_kwh(raw.get("new_initial_kwh", ""), errors, idx, "new_initial_kwh")
        if None in (mp, change_ts, old_meter, new_meter, old_final, new_initial):
            continue
        if old_meter == new_meter:
            errors.append(f"第 {idx} 行: 新旧表号相同 ({old_meter})")
            continue
        rec = ChangeRow(mp, change_ts, old_meter, new_meter, old_final, new_initial)
        key = (mp, change_ts)
        if key in seen:
            if seen[key] != rec:
                errors.append(f"第 {idx} 行: 文件内重复换表单 {key} 且内容不一致")
            continue
        seen[key] = rec
        out.append(rec)
    if errors:
        raise CsvError(errors[:MAX_ERRORS])
    return out
