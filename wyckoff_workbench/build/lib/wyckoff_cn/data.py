import csv
import hashlib
import io
import json
import math
import re
from datetime import date

from .common import ResearchError, SHANGHAI, digest, instant, iso_date, now, require_text

MODES = {"manual_observation_snapshot", "historical_reconstruction", "synthetic_demo"}
REQUIRED = {"symbol", "date", "open", "high", "low", "close", "volume"}
BAR_SCHEMA_VERSION = "daily-bar-2"


def number(value, field, optional=False):
    if value is None or value == "":
        if optional:
            return None
        raise ResearchError(field + "缺失；不补零")
    if isinstance(value, bool):
        raise ResearchError(field + "不能使用布尔值")
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ResearchError(field + "不是数字") from exc
    if not math.isfinite(result):
        raise ResearchError(field + "不得使用 NaN/Infinity")
    return result


def clean_row(row, metadata):
    symbol = require_text(row.get("symbol"), "symbol")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,24}", symbol):
        raise ResearchError("symbol只能使用字母数字及_.-")
    day = require_text(row.get("date"), "date")
    parsed_day = iso_date(day, "date")
    if parsed_day < date(1992, 1, 1):
        raise ResearchError("本版只接受1992年起的中国市场日期")
    result = {"symbol": symbol, "date": day}
    for field in ("open", "high", "low", "close", "volume", "amount"):
        result[field] = number(row.get(field), field, optional=field == "amount")
    result["previous_close"] = number(row.get("previous_close"), "previous_close", optional=True)
    o, h, l, c, v = (result[k] for k in ("open", "high", "low", "close", "volume"))
    if min(o, h, l, c) <= 0 or not l <= min(o, c) <= max(o, c) <= h or v < 0:
        raise ResearchError(symbol + " " + day + " OHLC/成交量不合法")
    if result["amount"] is not None and result["amount"] < 0:
        raise ResearchError("成交额不能小于零")
    if result["previous_close"] is not None and result["previous_close"] <= 0:
        raise ResearchError("previous_close必须为正数")
    complete = row.get("complete", "unknown")
    if complete not in (True, False, "true", "false", "unknown"):
        raise ResearchError("complete只能是true/false/unknown")
    result["complete"] = "true" if complete is True else "false" if complete is False else complete
    result["status"] = row.get("status") or "unknown"
    if result["status"] not in {"normal", "suspended", "restricted", "unknown"}:
        raise ResearchError("status只能是normal/suspended/restricted/unknown")
    source_at = row.get("source_available_at") or None
    basis = row.get("availability_basis") or "unknown"
    if source_at:
        if instant(source_at).astimezone(SHANGHAI).date() < parsed_day:
            raise ResearchError("来源可得时间早于行情日期")
        if basis == "unknown":
            raise ResearchError("source_available_at须配availability_basis")
    elif basis != "unknown":
        raise ResearchError("availability_basis不能代替缺失的来源时间")
    result.update(source_available_at=source_at, availability_basis=basis)
    result["source_revision_id"] = str(row.get("source_revision_id") or "unknown")
    return result


def import_csv(store, content, metadata):
    if len(content) > 25 * 1024 * 1024:
        raise ResearchError("单次导入上限25MiB")
    if metadata.get("record_mode", "manual_observation_snapshot") not in MODES:
        raise ResearchError("行情导入不允许补签prospective_shadow；请使用当前采集后的真实研究记录")
    source_id = require_text(metadata.get("source_id"), "source_id")
    volume_unit = metadata.get("volume_unit", "unknown")
    if volume_unit not in {"shares", "hands", "source_native", "unknown"}:
        raise ResearchError("volume_unit不合法")
    amount_unit = metadata.get("amount_unit", "unknown")
    if amount_unit not in {"yuan", "source_native", "unknown"}:
        raise ResearchError("amount_unit不合法")
    basis = metadata.get("price_basis", "unknown")
    if basis not in {"raw", "pit_comparable", "current_qfq", "unknown"}:
        raise ResearchError("price_basis不合法")
    stamp = now()
    captured = metadata.get("captured_at") or stamp
    if instant(captured) > instant(stamp):
        raise ResearchError("captured_at不能在未来")
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
        if not REQUIRED.issubset(set(reader.fieldnames or [])):
            raise ResearchError("CSV缺少必需字段: " + ",".join(sorted(REQUIRED)))
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ResearchError("CSV有重复列名")
        rows = []
        for row in reader:
            if None in row:
                raise ResearchError("CSV行字段多于表头；含逗号的文本须加引号")
            rows.append(clean_row(row, metadata))
    except UnicodeDecodeError as exc:
        raise ResearchError("CSV必须是UTF-8，可在Excel另存为CSV UTF-8") from exc
    if not rows:
        raise ResearchError("CSV没有数据")
    capture_day = instant(captured).astimezone(SHANGHAI).date()
    for row in rows:
        if iso_date(row["date"], "行情日期") > capture_day:
            raise ResearchError("行情日期晚于快照采集日期")
        if row["source_available_at"] and instant(row["source_available_at"]) > instant(captured):
            raise ResearchError("来源可得时间晚于快照采集时间")
    seen = set()
    previous = {}
    for row in rows:
        key = (row["symbol"], row["date"])
        if key in seen:
            raise ResearchError("同一快照有重复证券/日期，不静默去重")
        if row["date"] <= previous.get(row["symbol"], ""):
            raise ResearchError("同一证券日期必须严格递增，不静默排序")
        seen.add(key)
        previous[row["symbol"]] = row["date"]
    sha = hashlib.sha256(content).hexdigest()
    payload = {"source_id": source_id, "source_sha256": sha, "captured_at": captured,
               "ingested_at": stamp, "record_mode": metadata.get("record_mode", "manual_observation_snapshot"),
               "bar_schema_version": BAR_SCHEMA_VERSION,
               "volume_unit": volume_unit, "amount_unit": amount_unit, "price_basis": basis,
               "source_ref": metadata.get("source_ref") or "local_csv",
               "unit_basis": metadata.get("unit_basis") or "unknown",
               "source_acceptance": "not_certified", "rows": rows,
               "origin": metadata.get("origin"), "calendar_verified": False,
               "corporate_actions_verified": False, "pit_version_verified": False}
    snapshot_id = "snapshot-" + digest({"sha": sha, "metadata": metadata})[:20]
    existing = [r for r in store.records("snapshot") if r["record_id"] == snapshot_id]
    if existing:
        return existing[0]
    (store.raw / (sha + ".bin")).write_bytes(content)
    return store.append("snapshot", payload, snapshot_id)


def visible_rows(snapshot, symbol, as_of, decision_at, mode="manual_observation_snapshot"):
    instant(decision_at)
    cutoff = iso_date(as_of, "观察截止日")
    if cutoff > instant(decision_at).astimezone(SHANGHAI).date():
        raise ResearchError("观察截止日不能晚于决策时的上海日期")
    s = snapshot["payload"]
    if mode not in MODES | {"prospective_shadow"}:
        raise ResearchError("record_mode不合法")
    selected = [r for r in s["rows"] if r["symbol"] == symbol and iso_date(r["date"], "行情日期") <= cutoff]
    if not selected:
        raise ResearchError("截止日期没有该证券行情")
    if mode == "prospective_shadow":
        raise ResearchError("该版本未具备来源验收、冻结场景及实时采集器；真实Shadow被阻断")
    # Historical reconstruction cannot invent when a historical bar was known.
    historical = mode == "historical_reconstruction"
    if historical:
        for row in selected:
            if not row["source_available_at"]:
                raise ResearchError("历史决策缺少来源首次可得时间；可改为当前快照描述")
            if instant(row["source_available_at"]) > instant(decision_at):
                raise ResearchError("输入包含决策时尚不可得的行情")
        if not s["pit_version_verified"]:
            raise ResearchError("当前数据版本未通过历史PIT验收；禁止伪装成合法历史决策")
    else:
        if max(instant(s["captured_at"]), instant(s["ingested_at"])) > instant(decision_at):
            raise ResearchError("决策时间早于快照采集/本机导入时间，不允许补签")
    if any(row["complete"] != "true" for row in selected):
        raise ResearchError("输入含未声明完整的日线；不能用于冻结日频判断")
    return selected


def quality(snapshot):
    s = snapshot["payload"]
    rows = s["rows"]
    symbols = sorted({r["symbol"] for r in rows})
    result = {"snapshot_id": snapshot["record_id"], "source_id": s["source_id"],
            "captured_at": s["captured_at"], "ingested_at": s["ingested_at"],
            "source_sha256": s["source_sha256"], "price_basis": s["price_basis"],
            "bar_schema_version": s.get("bar_schema_version", "daily-bar-1"),
            "volume_unit": s["volume_unit"], "amount_unit": s["amount_unit"],
            "symbols": symbols, "row_count": len(rows),
            "first_date": min(r["date"] for r in rows), "last_date": max(r["date"] for r in rows),
            "missing_amount": sum(r["amount"] is None for r in rows),
            "zero_amount": sum(r["amount"] == 0 for r in rows),
            "missing_source_time": sum(r["source_available_at"] is None for r in rows),
            "rows_with_previous_close": sum(r.get("previous_close") is not None for r in rows),
            "unconfirmed_complete": sum(r["complete"] != "true" for r in rows),
            "unknown_status": sum(r["status"] == "unknown" for r in rows),
            "source_acceptance": "not_certified", "historical_pit": "not_certified",
            "calendar_completeness": "unknown", "corporate_actions": "unknown"}
    origin = s.get("origin")
    coverage = origin.get("market_coverage") if isinstance(origin, dict) else None
    if isinstance(coverage, dict):
        result["market_coverage"] = coverage
    return result
