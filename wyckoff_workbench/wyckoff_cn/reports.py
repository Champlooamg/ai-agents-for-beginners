import csv
import io
import json
import zipfile
from pathlib import Path

from .common import canonical, digest, instant, iso_date, now
from .data import quality
from .market import market_snapshot_errors
from .research import _candidate_key, calculate, candidate_view, gate
from .strategy import card_fingerprint


def audit(store):
    integrity = store.verify()
    errors = list(integrity["errors"])
    source_refs = {(r["payload"].get("source_sha256"), r["payload"].get("source_id"))
                    for r in store.records("source_capture")}
    market_snapshots_checked = 0
    for rec in store.records("snapshot"):
        origin = rec["payload"].get("origin")
        if isinstance(origin, dict) and origin.get("market_coverage") is not None:
            market_snapshots_checked += 1
            errors.extend("全市场快照审计失败 " + rec["record_id"] + ": " + message
                          for message in market_snapshot_errors(rec["payload"], source_refs))
    cards = store.records("card")
    for rec in cards:
        p = rec["payload"]
        try:
            s = store.get(p["snapshot_id"], "snapshot")["payload"]
            protocol = store.get(p["protocol_id"], "protocol")["payload"]
            if instant(p["last_dependency_at"]) > instant(p["decision_at"]):
                errors.append("研究卡时间泄漏: " + rec["record_id"])
            if instant(rec["recorded_at"]) < instant(p["decision_at"]):
                errors.append("研究卡冻结时间非法: " + rec["record_id"])
            cutoff = iso_date(p["as_of"], "研究卡观察截止日")
            rows = [r for r in s["rows"] if r["symbol"] == p["symbol"] and iso_date(r["date"], "行情日期") <= cutoff]
            bm = [r for r in s["rows"] if r["symbol"] == p["benchmark_symbol"] and iso_date(r["date"], "行情日期") <= cutoff] if p["benchmark_symbol"] else None
            rebuilt = calculate(rows, p["snapshot_id"], protocol, bm)
            if digest(rebuilt) != digest(p["facts"]):
                errors.append("冻结事实无法按协议复算: " + rec["record_id"])
            refs = {e["evidence_id"] for e in p["evidence"]}
            if not set(p["manual"]["supports"] + p["manual"]["opposes"]).issubset(refs):
                errors.append("证据引用错误: " + rec["record_id"])
        except Exception as exc:
            errors.append("研究卡审计失败: " + rec["record_id"] + " " + str(exc))
    candidates = {r["record_id"]: r for r in store.records("candidate")}
    attempts = {r["record_id"]: r for r in store.records("candidate_attempt")}
    candidate_events = store.records("candidate_event")
    events_by_candidate = {}
    for event in candidate_events:
        events_by_candidate.setdefault(event["payload"]["candidate_id"], []).append(event)
    for candidate_id, rec in candidates.items():
        p = rec["payload"]
        try:
            iso_date(p["expiry_date"], "候选到期日")
            if p.get("candidate_key"):
                structure_start = iso_date(p["structure_start_date"], "结构起始日")
                card_for_candidate = store.get(p["card_id"], "card")["payload"]
                if structure_start > iso_date(card_for_candidate["last_bar_date"]):
                    errors.append("候选结构起始日晚于登记行情: " + candidate_id)
                expected_key = _candidate_key(p["symbol"], p["protocol_id"], p["scenario"],
                                              p["support"], p["resistance"], p["structure_start_date"])
                if p["candidate_key"] != expected_key:
                    errors.append("候选结构身份与冻结边界不一致: " + candidate_id)
            attempt_id = p.get("candidate_attempt_id")
            if attempt_id and (attempt_id not in attempts or attempts[attempt_id]["payload"].get("status") != "accepted"):
                errors.append("候选登记尝试引用错误: " + candidate_id)
            prior_card_id = p["card_id"]
            prior_card = store.get(prior_card_id, "card")["payload"]
            prior_state = "registered"
            dependency_after = instant(p["registered_at"])
            for event in events_by_candidate.get(candidate_id, []):
                ep = event["payload"]
                current_card = store.get(ep["card_id"], "card")["payload"]
                prior_date = iso_date(prior_card["last_bar_date"], "上次行情日期")
                event_date = iso_date(ep["event_date"], "候选观察日期")
                if ep.get("previous_state") != prior_state:
                    errors.append("候选状态链不连续: " + event["record_id"])
                if ep.get("previous_card_id") and ep["previous_card_id"] != prior_card_id:
                    errors.append("候选前序研究卡引用错误: " + event["record_id"])
                if current_card["snapshot_id"] == prior_card["snapshot_id"]:
                    errors.append("候选状态缺少新的行情快照根: " + event["record_id"])
                if instant(current_card["last_dependency_at"]) <= dependency_after:
                    errors.append("候选状态行情并非在登记/上次决策后采集: " + event["record_id"])
                if event_date <= prior_date:
                    errors.append("候选状态缺少更新的行情日期: " + event["record_id"])
                if ep.get("snapshot_id") and ep["snapshot_id"] != current_card["snapshot_id"]:
                    errors.append("候选观察快照引用错误: " + event["record_id"])
                evidence_id = ep.get("new_bar_evidence_id")
                evidence = next((x for x in current_card["evidence"] if x["evidence_id"] == evidence_id), None)
                if (not evidence or evidence["symbol"] != p["symbol"] or
                        evidence["snapshot_id"] != current_card["snapshot_id"] or
                        iso_date(evidence["event_date"], "新行情证据日期") != event_date or
                        evidence_id not in ep.get("evidence_ids", [])):
                    errors.append("候选状态缺少可追溯的新行情证据: " + event["record_id"])
                if not instant(ep["last_dependency_at"]) <= instant(ep["confirmed_at"]) <= instant(ep["decision_at"]):
                    errors.append("候选时间链非法: " + event["record_id"])
                if ep.get("last_dependency_at") != current_card["last_dependency_at"]:
                    errors.append("候选状态依赖时间与研究卡不符: " + event["record_id"])
                dependency_after = instant(ep["decision_at"])
                prior_card_id = ep["card_id"]
                prior_card = current_card
                prior_state = ep["new_state"]
        except Exception as exc:
            errors.append("候选审计失败: " + candidate_id + " " + str(exc))
    for attempt_id, rec in attempts.items():
        p = rec["payload"]
        try:
            card = store.get(p["card_id"], "card")["payload"]
            if p["snapshot_id"] != card["snapshot_id"]:
                errors.append("候选登记尝试快照引用错误: " + attempt_id)
            if p.get("candidate_key"):
                iso_date(p["structure_start_date"], "登记尝试结构起始日")
            if p["status"] in {"rejected_duplicate", "rejected_structure_revision"}:
                if not p.get("related_candidate_ids") or any(x not in candidates for x in p["related_candidate_ids"]):
                    errors.append("被拒绝候选尝试缺少关联原记录: " + attempt_id)
                elif p["status"] == "rejected_duplicate" and not any(
                        candidates[x]["payload"].get("candidate_key") == p.get("candidate_key")
                        for x in p["related_candidate_ids"]):
                    errors.append("重复候选尝试的结构身份不匹配: " + attempt_id)
            elif p["status"] == "accepted":
                linked = [c for c in candidates.values() if c["payload"].get("candidate_attempt_id") == attempt_id]
                if not linked:
                    errors.append("已接受的候选尝试没有对应候选: " + attempt_id)
                elif linked[0]["payload"].get("candidate_key") != p.get("candidate_key"):
                    errors.append("已接受候选与登记尝试结构身份不匹配: " + attempt_id)
            else:
                errors.append("候选登记尝试状态无效: " + attempt_id)
        except Exception as exc:
            errors.append("候选登记尝试审计失败: " + attempt_id + " " + str(exc))
    strategy_cards = store.records("strategy_card")
    for rec in strategy_cards:
        p = rec["payload"]
        try:
            if p.get("production_permission") is not False:
                errors.append("条件策略卡生产权限非法: " + rec["record_id"])
            if p.get("target_price", {}).get("value") is not None:
                errors.append("条件策略卡给出未裁决目标价: " + rec["record_id"])
            if p.get("risk", {}).get("position_size") is not None:
                errors.append("条件策略卡给出未裁决仓位: " + rec["record_id"])
            if p.get("interpretation_status") != "conditional_unvalidated":
                errors.append("条件策略卡有效性等级非法: " + rec["record_id"])
            if p.get("card_fingerprint") != card_fingerprint(p):
                errors.append("条件策略卡指纹无法复算: " + rec["record_id"])
            if instant(rec["recorded_at"]) < instant(p["decision_moment"]):
                errors.append("条件策略卡冻结时间非法: " + rec["record_id"])
        except Exception as exc:
            errors.append("条件策略卡审计失败: " + rec["record_id"] + " " + str(exc))
    return {"ok": not errors, "errors": errors, "integrity": integrity,
            "frozen_cards_checked": len(cards), "strategy_cards_checked": len(strategy_cards),
            "checked_at": now(),
            "market_snapshots_checked": market_snapshots_checked,
            "historical_pit_certified": False, "strategy_effectiveness": "not_run",
            "scope": "本机哈希/引用/已冻结事实复算/时间链；不等于供应商数据真实性验收"}


def fmt(value):
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return f"{value:.8g}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def card_markdown(record):
    p = record["payload"]
    f = p["facts"][-1]
    lines = [f"# {p['symbol']} 威科夫研究卡（描述版）", "",
             f"冻结时间：{p['decision_at']}；请求截止：{p['as_of']}；最后报价：{p['last_bar_date']}。",
             f"记录模式：{p['record_mode']}。快照：{p['snapshot_id']}。协议：{p['protocol_id']}。",
             f"研究卡ID：{record['record_id']}；账本哈希：{record['record_hash']}。", "",
             "## 可复算事实", "", "| 字段 | 值 |", "|---|---|",
             *[f"| {k} | {fmt(f.get(k))} |" for k in
               ("open", "high", "low", "close", "previous_close", "previous_close_basis", "volume", "amount", "range", "body", "close_location", "true_range", "gap_open", "net_progress", "volume_reference", "relative_volume", "relative_strength_log")],
             "", f"事实证据ID：{f['evidence_id']}。成交量单位：{p['data_quality']['volume_unit']}。",
             "量比基准排除当前报价根；相对强弱为同日期端点的对数收益差。窗口按报价根数计，交易日完整性未验收。",
             "", "## 人工判断（未验证）", ""]
    for key, label in (("market_context", "市场背景"), ("hypothesis", "主假设"),
                       ("alternative_hypothesis", "竞争解释"), ("invalidate_if", "证伪条件"),
                       ("expected_response", "待观察响应"), ("risk_review", "独立风险复核"),
                       ("next_review", "下一次观察")):
        lines.append(f"**{label}**：{p['manual'][key]}\n")
    lines.extend(["支持引用：" + "; ".join(p["manual"]["supports"]),
                  "反对引用：" + ("; ".join(p["manual"]["opposes"]) or "未登记；不等于没有反对证据"),
                  "", "## 数据限制与闸口", "", *["- " + x for x in p["limitations"]],
                  "", "回测闸口：BLOCK。六道验证：全部not_run。没有模拟成交、胜率或收益结论。", ""])
    return "\n".join(lines)


def daily_report(store):
    checks = audit(store)
    lines = ["# 中国市场威科夫研究工作台运行报告", "",
             "生成时间：" + now(), "",
             "本报告核对软件运行与研究记录，不证明策略有效性。", "",
             "## 数据快照", "", "| 快照 | 证券 | 行数 | 日期范围 | 缺成交额 | 缺来源时点 |", "|---|---|---:|---|---:|---:|"]
    for r in store.records("snapshot"):
        q = quality(r)
        lines.append(f"| {r['record_id']} | {','.join(q['symbols'])} | {q['row_count']} | {q['first_date']}—{q['last_date']} | {q['missing_amount']} | {q['missing_source_time']} |")
    lines.extend(["", "## 研究记录", "",
                  f"冻结卡 {len(store.records('card'))}；候选 {len(store.records('candidate'))}；登记尝试 {len(store.records('candidate_attempt'))}；风险记录 {len(store.records('risk'))}；订单计划 {len(store.records('order_plan'))}。",
                  "候选是人工观察记录；同证券、协议、结构起始日、支撑阻力边界和场景只登记一次，改写事件族或结构文本不产生独立样本。", "",
                  "| 候选 | 证券 | 场景 | 状态 | 事件族 |", "|---|---|---|---|---|"])
    for c in candidate_view(store):
        lines.append("| " + " | ".join(fmt(c[k]) for k in ("candidate_id", "symbol", "scenario", "state", "event_family_id")) + " |")
    lines.extend(["", "## 审计", "", "本机审计：" + ("通过" if checks["ok"] else "失败"),
                  checks["scope"], *["- " + e for e in checks["errors"]],
                  "", "## 尚未通过的有效性前提", "", *["- " + x for x in gate(store)["blockers"]],
                  "", "回测与真实前瞻Shadow：BLOCK；结果有效性：not_run；生产与自动订单：关闭。", ""])
    return "\n".join(lines)


def csv_records(records):
    fields = ["seq", "record_id", "recorded_at", "record_hash", "previous_hash", "payload_json"]
    f = io.StringIO(newline="")
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader()
    for r in records:
        writer.writerow({**{k: r[k] for k in fields[:-1]}, "payload_json": canonical(r["payload"])})
    return "\ufeff" + f.getvalue()


def export_bundle(store):
    """Export current state as a consistent ledger and independent cards."""
    output = io.BytesIO()
    records = store.records()
    hashes = {}
    def write(z, name, value):
        raw = value.encode("utf-8") if isinstance(value, str) else value
        z.writestr(name, raw)
        import hashlib
        hashes[name] = hashlib.sha256(raw).hexdigest()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        write(z, "ledger.json", json.dumps(records, ensure_ascii=False, indent=2))
        for kind in sorted({r["kind"] for r in records}):
            write(z, "tables/" + kind + ".csv", csv_records([r for r in records if r["kind"] == kind]))
        for r in store.records("card"):
            write(z, "cards/" + r["record_id"] + ".md", card_markdown(r))
        for r in store.records("snapshot"):
            sha = r["payload"]["source_sha256"]
            write(z, "raw/" + sha + ".csv", (store.raw / (sha + ".bin")).read_bytes())
        for r in store.records("source_capture"):
            sha = r["payload"]["source_sha256"]
            write(z, "source_responses/" + sha + ".bin", (store.raw / (sha + ".bin")).read_bytes())
        for r in store.records("external_note"):
            sha = r["payload"]["content_sha256"]
            write(z, "external_notes/" + sha + ".txt", (store.raw / ("external-" + sha + ".txt")).read_bytes())
        write(z, "运行报告.md", daily_report(store))
        write(z, "audit.json", json.dumps(audit(store), ensure_ascii=False, indent=2))
        z.writestr("export_manifest.json", json.dumps({"generated_at": now(), "sha256": hashes}, ensure_ascii=False, indent=2))
    return output.getvalue()
