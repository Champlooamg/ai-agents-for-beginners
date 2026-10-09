"""Untrusted output intake from companion research tools.

Imported text is archived for review only. It never becomes market evidence,
an analysis input, a candidate, or an order.
"""
import hashlib
from datetime import date

from .common import ResearchError, digest, instant, now, require_text, SHANGHAI

TOOLS = {
    "panwatch": "PanWatch",
    "tradingagents_astock": "TradingAgents-Astock",
    "tickflow_stock_panel": "TickFlow Stock Panel",
    "global_stock_data": "Global Stock Data",
    "finance_quant_skills": "Finance Quant Skills",
    "other": "Other",
}
MAX_NOTE_BYTES = 1024 * 1024


def add_external_note(store, request):
    tool = request.get("source_tool")
    if tool not in TOOLS:
        raise ResearchError("source_tool不在允许列表")
    title = require_text(request.get("title"), "标题")
    if len(title) > 300:
        raise ResearchError("标题长度上限300字符")
    content = request.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ResearchError("外部输出内容不能为空")
    try:
        raw = content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ResearchError("外部输出包含无效Unicode字符") from exc
    if len(raw) > MAX_NOTE_BYTES:
        raise ResearchError("单个外部输出上限1MiB")
    if "\x00" in content:
        raise ResearchError("外部输出含NUL字符")
    reported_date = request.get("source_reported_date") or "unknown"
    if reported_date != "unknown":
        try:
            if date.fromisoformat(reported_date).isoformat() != reported_date:
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise ResearchError("来源报告日期须为YYYY-MM-DD或unknown") from exc
        if date.fromisoformat(reported_date) > instant(now()).astimezone(SHANGHAI).date():
            raise ResearchError("来源报告日期不能在未来")
    source_ref = request.get("source_ref") or "unknown"
    if not isinstance(source_ref, str) or len(source_ref) > 2048 or "\x00" in source_ref:
        raise ResearchError("来源引用格式不合法或过长")
    sha = hashlib.sha256(raw).hexdigest()
    captured_at = now()
    payload = {
        "source_tool": tool,
        "source_tool_label": TOOLS[tool],
        "title": title,
        "source_reported_date": reported_date,
        "source_ref": source_ref,
        "captured_at": captured_at,
        "content_sha256": sha,
        "content_bytes": len(raw),
        "raw_file": "raw/external-" + sha + ".txt",
        "qualification": "unverified_external_output_not_market_evidence_not_analysis_input",
    }
    note_id = "external-note-" + digest(payload)[:20]
    existing = [r for r in store.records("external_note") if r["record_id"] == note_id]
    if existing:
        return existing[0]
    path = store.raw / ("external-" + sha + ".txt")
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        raise ResearchError("同哈希外部文件已存在但内容不符")
    if not path.exists():
        path.write_bytes(raw)
    return store.append("external_note", payload, note_id)


def external_note_summary(record):
    return {key: record["payload"][key] for key in (
        "source_tool", "source_tool_label", "title", "source_reported_date",
        "source_ref", "captured_at", "content_sha256", "content_bytes", "qualification")}


def external_note_text(store, note_id):
    record = store.get(note_id, "external_note")
    p = record["payload"]
    path = store.raw / ("external-" + p["content_sha256"] + ".txt")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ResearchError("外部输出原文缺失") from exc
    if hashlib.sha256(raw).hexdigest() != p["content_sha256"]:
        raise ResearchError("外部输出原文哈希不一致")
    return raw.decode("utf-8")
