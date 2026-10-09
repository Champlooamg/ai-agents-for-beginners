import math
import uuid

from .common import ResearchError, digest, instant, iso_date, now, require_text
from .data import number, quality, visible_rows

DEFAULT_PROTOCOL = {
    "version": "descriptive-facts-2",
    "purpose": "descriptive_only",
    "volume_window": 20,
    "relative_strength_window": 20,
    "window_unit": "quoted_rows_not_certified_sessions",
    "current_bar_in_volume_reference": False,
    "technical_theory": "wyckoff_only",
    "optimized": False,
    "performance_validation": "not_run",
}


def freeze_protocol(store, payload=None):
    p = dict(DEFAULT_PROTOCOL if payload is None else payload)
    # Persisted version-1 protocols retain their explicit version and replay
    # unchanged. Newly created protocols without a version use the current one.
    p.setdefault("version", DEFAULT_PROTOCOL["version"])
    if p["version"] not in {"descriptive-facts-1", "descriptive-facts-2"}:
        raise ResearchError("不支持的事实公式版本")
    if p.get("purpose") != "descriptive_only" or p.get("technical_theory") != "wyckoff_only":
        raise ResearchError("首版协议仅用于纯威科夫事实描述")
    for key in ("volume_window", "relative_strength_window"):
        if type(p.get(key)) is not int or not 1 <= p[key] <= 500:
            raise ResearchError(key + "须为1—500的整数")
    if p.get("current_bar_in_volume_reference") is not False or p.get("optimized") is not False:
        raise ResearchError("禁止纳入当前量及宣称已优化")
    p["performance_validation"] = "not_run"
    p["window_unit"] = "quoted_rows_not_certified_sessions"
    key = "protocol-" + digest(p)[:20]
    existing = [r for r in store.records("protocol") if r["record_id"] == key]
    return existing[0] if existing else store.append("protocol", p, key)


def evidence_id(snapshot_id, row):
    return "bar-" + digest({"snapshot_id": snapshot_id, "row": row})[:24]


def calculate(rows, snapshot_id, protocol, benchmark_rows=None):
    """All calculations use the current and preceding rows; no centered windows.

    Missing dates are not silently filled; window lengths count observed rows.
    Version 2 may use an input row's previous_close only when no prior input row
    exists. Raw-price returns across corporate actions are descriptive and qualified.
    """
    w = protocol["volume_window"]
    rsw = protocol["relative_strength_window"]
    formula_version = protocol.get("version", "descriptive-facts-1")
    if formula_version not in {"descriptive-facts-1", "descriptive-facts-2"}:
        raise ResearchError("不支持的事实公式版本")
    benchmark = {r["date"]: r for r in (benchmark_rows or [])}
    output = []
    for i, row in enumerate(rows):
        prev = rows[i - 1] if i else None
        previous_close = (prev["close"] if prev else
                          row.get("previous_close") if formula_version == "descriptive-facts-2" else None)
        spread = row["high"] - row["low"]
        reference_rows = rows[i - w:i] if i >= w else []
        mean_volume = sum(r["volume"] for r in reference_rows) / w if reference_rows else None
        dep_rows = rows[max(0, i - max(w, rsw, 1)):i + 1]
        refs = [evidence_id(snapshot_id, r) for r in dep_rows]
        facts = {
            "date": row["date"], "symbol": row["symbol"],
            "evidence_id": evidence_id(snapshot_id, row),
            "dependency_ids": refs, "open": row["open"], "high": row["high"],
            "low": row["low"], "close": row["close"], "volume": row["volume"],
            "amount": row["amount"], "range": spread,
            "body": abs(row["close"] - row["open"]),
            "signed_body": row["close"] - row["open"],
            "upper_wick": row["high"] - max(row["open"], row["close"]),
            "lower_wick": min(row["open"], row["close"]) - row["low"],
            "close_location": (row["close"] - row["low"]) / spread if spread else None,
            "true_range": max(spread, abs(row["high"] - previous_close),
                              abs(row["low"] - previous_close)) if previous_close is not None else None,
            "gap_open": row["open"] / previous_close - 1 if previous_close is not None else None,
            "net_progress": row["close"] / previous_close - 1 if previous_close is not None else None,
            "volume_reference": mean_volume,
            "relative_volume": row["volume"] / mean_volume if mean_volume and mean_volume > 0 else None,
            "relative_strength_log": None, "benchmark_dependency_dates": [],
            "complete": row["complete"], "limit_state": "unknown",
            "missing_dependencies": [],
        }
        if formula_version == "descriptive-facts-2":
            facts["previous_close"] = previous_close
            facts["previous_close_basis"] = ("previous_input_row" if prev else
                "row_previous_close" if previous_close is not None else "missing")
            facts["previous_observation_date"] = prev["date"] if prev else None
        if not prev:
            if formula_version == "descriptive-facts-2" and previous_close is not None:
                facts["missing_dependencies"].append("previous_bar_ohlc")
            else:
                facts["missing_dependencies"].append("previous_bar")
        if mean_volume is None:
            facts["missing_dependencies"].append("volume_warmup")
        elif mean_volume == 0:
            facts["missing_dependencies"].append("positive_volume_reference")
        if row["amount"] is None:
            facts["missing_dependencies"].append("actual_amount")
        if i >= rsw:
            first = rows[i - rsw]
            b0, b1 = benchmark.get(first["date"]), benchmark.get(row["date"])
            if b0 and b1:
                facts["relative_strength_log"] = math.log(row["close"] / first["close"]) - math.log(b1["close"] / b0["close"])
                facts["benchmark_dependency_dates"] = [first["date"], row["date"]]
                facts["dependency_ids"].extend([evidence_id(snapshot_id, b0), evidence_id(snapshot_id, b1)])
            else:
                facts["missing_dependencies"].append("benchmark_endpoints")
        else:
            facts["missing_dependencies"].append("relative_strength_warmup")
        output.append(facts)
    return output


def analysis(store, request):
    snapshot = store.get(request.get("snapshot_id"), "snapshot")
    protocol = store.get(request.get("protocol_id"), "protocol")
    mode = request.get("record_mode", "manual_observation_snapshot")
    if mode == "synthetic_demo" and snapshot["payload"]["record_mode"] != "synthetic_demo":
        raise ResearchError("真实来源不能改标synthetic_demo")
    if snapshot["payload"]["record_mode"] == "synthetic_demo" and mode != "synthetic_demo":
        raise ResearchError("合成数据不能改标真实来源")
    symbol = require_text(request.get("symbol"), "symbol")
    as_of = iso_date(require_text(request.get("as_of"), "as_of"), "观察截止日").isoformat()
    decision = request.get("decision_at") or now()
    if instant(decision) > instant(now()):
        raise ResearchError("决策时间不能在未来")
    rows = visible_rows(snapshot, symbol, as_of, decision, mode)
    benchmark_rows = None
    benchmark_id = request.get("benchmark_symbol")
    if benchmark_id:
        if benchmark_id == symbol:
            raise ResearchError("强弱基准不能与被研究证券相同")
        benchmark_rows = visible_rows(snapshot, benchmark_id, as_of, decision, mode)
    facts = calculate(rows, snapshot["record_id"], protocol["payload"], benchmark_rows)
    s = snapshot["payload"]
    issues = ["历史PIT版本未验收", "交易日历完整性未知", "公司行动/历史状态未验收",
              "当前仅允许描述；不输出买卖概率或绩效"]
    if s["price_basis"] != "pit_comparable":
        issues.append("使用原始或未验收价格；跨除权比较可能失真")
    if s["volume_unit"] == "unknown":
        issues.append("成交量单位未知；量比仅在同源单位保持一致的前提下成立")
    if s["unit_basis"] == "unknown":
        issues.append("单位来源说明未验收")
    if rows[-1]["date"] != as_of:
        issues.append("截止日无行情；仅显示最后有报价日期，不能判断是休市/停牌/缺根")
    evidence = []
    for row in rows + (benchmark_rows or []):
        evidence.append({"evidence_id": evidence_id(snapshot["record_id"], row),
                         "symbol": row["symbol"], "event_date": row["date"],
                         "source_available_at": row["source_available_at"],
                         "availability_basis": row["availability_basis"],
                         "ingested_at": s["ingested_at"],
                         "available_at": max(s["captured_at"], s["ingested_at"], key=instant),
                         "evidence_status": "observed", "snapshot_id": snapshot["record_id"]})
    return {"snapshot_id": snapshot["record_id"], "protocol_id": protocol["record_id"],
            "symbol": symbol, "as_of": as_of, "last_bar_date": rows[-1]["date"],
            "decision_at": decision, "record_mode": mode,
            "last_dependency_at": max(s["captured_at"], s["ingested_at"], key=instant),
            "formula_version": protocol["payload"].get("version", "descriptive-facts-1"), "facts": facts, "evidence": evidence,
            "benchmark_symbol": benchmark_id or None,
            "data_quality": quality(snapshot), "limitations": issues,
            "backtest_status": "BLOCK", "validation_gates": {key: "not_run" for key in
                ("redundancy", "conditional_increment", "ablation", "out_of_sample", "stability", "complexity_benefit")}}


def freeze_card(store, request):
    a = analysis(store, request)
    # The workstation, rather than the client, timestamps frozen decisions.
    if request.get("decision_at"):
        raise ResearchError("冻结研究卡使用服务器实际时间；不允许自填旧决策时刻")
    fields = ("market_context", "hypothesis", "alternative_hypothesis", "invalidate_if",
              "expected_response", "risk_review", "next_review")
    manual = {k: require_text(request.get(k), k) for k in fields}
    valid = {x["evidence_id"] for x in a["evidence"]}
    for key in ("supports", "opposes"):
        values = request.get(key, [])
        if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
            raise ResearchError(key + "必须是证据ID列表")
        if len(values) != len(set(values)) or not set(values).issubset(valid):
            raise ResearchError(key + "存在重复或无法解析的证据")
        manual[key] = values
    if not manual["supports"]:
        raise ResearchError("至少引用一条可解析的事实；它不代表独立交易确认")
    if set(manual["supports"]) & set(manual["opposes"]):
        raise ResearchError("同一事实同时支持与反对时，请在竞争解释说明中记录，不能重复计票")
    manual["annotator"] = request.get("annotator") or "local_user"
    manual["interpretation_status"] = "human_unvalidated"
    supersedes = request.get("supersedes_id") or None
    if supersedes:
        old = store.get(supersedes, "card")["payload"]
        if old["symbol"] != a["symbol"]:
            raise ResearchError("修订不得换证券")
        manual["revision_reason"] = require_text(request.get("revision_reason"), "revision_reason")
    a.update(manual=manual, supersedes_id=supersedes, production_permission=False)
    # Payload includes only selected prefix facts, never hidden future bars.
    return store.append("card", a)


TRANSITIONS = {
    "registered": {"context_pending", "context_established", "invalidated", "expired"},
    "context_pending": {"context_established", "invalidated", "expired"},
    "context_established": {"awaiting_test", "test_observed", "invalidated", "expired"},
    "awaiting_test": {"test_observed", "invalidated", "expired"},
    "test_observed": {"response_observed", "invalidated", "expired"},
    "response_observed": {"invalidated"}, "invalidated": set(), "expired": set(),
}


def candidate_view(store):
    result = []
    events = store.records("candidate_event")
    # Ledger records are ordered by sequence, so the last event for a candidate
    # is its current read-model state. Build the index once instead of scanning
    # the complete event ledger for every candidate.
    latest_event_by_candidate = {
        event["payload"]["candidate_id"]: event for event in events
    }
    for r in store.records("candidate"):
        payload = dict(r["payload"], candidate_id=r["record_id"], state="registered")
        event = latest_event_by_candidate.get(r["record_id"])
        if event:
            payload["state"] = event["payload"]["new_state"]
            payload["last_event_at"] = event["recorded_at"]
        result.append(payload)
    return result


def _structure_fingerprint(symbol, protocol_id, support, resistance, structure_start_date):
    # User-entered family/structure labels are descriptive metadata, not unique
    # identities. The structural origin date and frozen price bounds define the
    # reproducible scope while allowing a later, genuinely new range at the same levels.
    return digest({"symbol": symbol, "protocol_id": protocol_id,
                   "support": support, "resistance": resistance,
                   "structure_start_date": structure_start_date})


def _candidate_key(symbol, protocol_id, scenario, support, resistance, structure_start_date):
    return digest({"structure_fingerprint": _structure_fingerprint(
        symbol, protocol_id, support, resistance, structure_start_date), "scenario": scenario})


def _normalized_structure_id(value):
    return " ".join(value.casefold().split())


def register_candidate(store, request):
    card = store.get(request.get("card_id"), "card")["payload"]
    scenario = request.get("scenario")
    if scenario not in {"W-LPS-BU", "W-SPRING-TEST"}:
        raise ResearchError("候选场景只允许W-LPS-BU / W-SPRING-TEST")
    family = require_text(request.get("event_family_id"), "event_family_id")
    structure_id = require_text(request.get("structure_id"), "structure_id")
    support = number(request.get("support"), "support")
    resistance = number(request.get("resistance"), "resistance")
    structure_start_date = iso_date(require_text(request.get("structure_start_date"), "structure_start_date"),
                                    "结构起始日").isoformat()
    if not 0 < support < resistance:
        raise ResearchError("人工结构边界须0<support<resistance")
    if iso_date(structure_start_date) > iso_date(card["last_bar_date"]):
        raise ResearchError("结构起始日不能晚于候选登记时的最后行情日期")
    expiry = iso_date(require_text(request.get("expiry_date"), "expiry_date"), "到期日").isoformat()
    if iso_date(expiry) <= iso_date(card["last_bar_date"]):
        raise ResearchError("到期日须晚于登记时最后报价日；仅作描述性日历期限")
    rules = {key: require_text(request.get(key), key)
             for key in ("test_rule", "response_rule", "invalidation_rule")}
    stable_structure_id = _normalized_structure_id(structure_id)
    fingerprint = _structure_fingerprint(card["symbol"], card["protocol_id"], support, resistance,
                                         structure_start_date)
    candidate_key = _candidate_key(card["symbol"], card["protocol_id"], scenario, support, resistance,
                                   structure_start_date)
    existing_candidates = candidate_view(store)
    duplicate = []
    structure_conflict = []
    for existing in existing_candidates:
        if existing["symbol"] != card["symbol"] or existing.get("protocol_id") != card["protocol_id"]:
            continue
        old_key = existing.get("candidate_key") or _candidate_key(
            existing["symbol"], existing["protocol_id"], existing["scenario"],
            existing["support"], existing["resistance"], existing.get("structure_start_date") or
            store.get(existing["card_id"], "card")["payload"]["last_bar_date"])
        if old_key == candidate_key:
            duplicate.append(existing["candidate_id"])
        if (_normalized_structure_id(existing.get("structure_id", "")) == stable_structure_id and
                (existing["support"] != support or existing["resistance"] != resistance or
                 (existing.get("structure_start_date") and existing["structure_start_date"] != structure_start_date))):
            structure_conflict.append(existing["candidate_id"])
    attempt_id = "candidate-attempt-" + uuid.uuid4().hex[:16]
    attempt_status = "accepted" if not duplicate and not structure_conflict else (
        "rejected_duplicate" if duplicate else "rejected_structure_revision")
    attempt = store.append("candidate_attempt", {
        "attempt_id": attempt_id, "card_id": request["card_id"],
        "snapshot_id": card["snapshot_id"], "symbol": card["symbol"],
        "protocol_id": card["protocol_id"], "scenario": scenario,
        "event_family_id": family, "structure_id": structure_id,
        "structure_start_date": structure_start_date,
        "structure_fingerprint": fingerprint, "candidate_key": candidate_key,
        "support": support, "resistance": resistance, "status": attempt_status,
        "related_candidate_ids": sorted(set(duplicate + structure_conflict)),
        "reason": ("同一结构/场景已登记；family/structure文本改名不创建独立候选"
                   if duplicate else "稳定结构ID已登记，但本次边界不同" if structure_conflict else None)})
    if duplicate:
        raise ResearchError("同证券/同结构/同场景已登记；本次重复尝试已留在账本（" + attempt["record_id"] + "），修改事件族或结构文本不会创建新样本")
    if structure_conflict:
        raise ResearchError("稳定结构ID已用于不同边界；本次冲突已留在账本（" + attempt["record_id"] + "），请先重新定义结构")
    payload = {"symbol": card["symbol"], "scenario": scenario,
               "event_family_id": family, "structure_id": structure_id,
               "structure_revision_id": uuid.uuid4().hex[:16],
               "structure_start_date": structure_start_date,
               "structure_fingerprint": fingerprint, "candidate_key": candidate_key,
               "candidate_attempt_id": attempt["record_id"],
               "card_id": request["card_id"], "snapshot_id": card["snapshot_id"],
               "protocol_id": card["protocol_id"], "support": support, "resistance": resistance,
               "registered_at": now(), "expiry_date": expiry,
               "record_mode": card["record_mode"], "state": "registered",
               "boundary_status": "human_unvalidated", "observation_only": True,
               "boundary_evidence_ids": card["manual"]["supports"]}
    payload.update(rules)
    payload["rule_hash"] = digest({k: payload[k] for k in
                                  ("support", "resistance", "expiry_date", "test_rule", "response_rule", "invalidation_rule")})
    return store.append("candidate", payload)


def transition_candidate(store, request):
    candidate_id = request.get("candidate_id")
    store.get(candidate_id, "candidate")
    c = next(x for x in candidate_view(store) if x["candidate_id"] == candidate_id)
    target = request.get("new_state")
    if target not in TRANSITIONS[c["state"]]:
        raise ResearchError("非法状态转换: " + c["state"] + " → " + str(target))
    card_rec = store.get(request.get("card_id"), "card")
    card = card_rec["payload"]
    if card["symbol"] != c["symbol"] or card["protocol_id"] != c["protocol_id"] or card["record_mode"] != c["record_mode"]:
        raise ResearchError("状态更新须同证券、同事实协议、同记录模式")
    if instant(card["decision_at"]) < instant(c["registered_at"]):
        raise ResearchError("更新状态必须使用候选登记之后新冻结的研究卡")
    initial_card = store.get(c["card_id"], "card")["payload"]
    prior_card_id = c["card_id"]
    prior_card = initial_card
    prior_events = [r for r in store.records("candidate_event") if r["payload"]["candidate_id"] == candidate_id]
    evidence_after = instant(c["registered_at"])
    if prior_events:
        if instant(card["decision_at"]) <= instant(prior_events[-1]["payload"]["decision_at"]):
            raise ResearchError("每次状态变化使用新冻结的研究卡，不重复使用旧判断")
        evidence_after = max(evidence_after, instant(prior_events[-1]["payload"]["decision_at"]))
        prior_card_id = prior_events[-1]["payload"]["card_id"]
        prior_card = store.get(prior_card_id, "card")["payload"]
    if instant(card["last_dependency_at"]) <= evidence_after:
        raise ResearchError("状态观察行情必须在候选登记/上次决策之后才被采集或导入")
    if card["snapshot_id"] == prior_card["snapshot_id"]:
        raise ResearchError("状态观察必须来自新的行情快照根")
    if iso_date(card["last_bar_date"]) <= iso_date(prior_card["last_bar_date"]):
        raise ResearchError("每次状态变化必须使用严格更新的行情日期")
    supported = set(card["manual"]["supports"])
    fresh_bar = next((e for e in card["evidence"]
                      if e["evidence_id"] in supported and e["symbol"] == c["symbol"]
                      and e["snapshot_id"] == card["snapshot_id"]
                      and iso_date(e["event_date"]) == iso_date(card["last_bar_date"])
                      and iso_date(e["event_date"]) > iso_date(prior_card["last_bar_date"])), None)
    if fresh_bar is None:
        raise ResearchError("状态观察须引用新快照中晚于上次观察的本证券最新行情证据")
    if target == "expired" and iso_date(card["as_of"]) < iso_date(c["expiry_date"]):
        raise ResearchError("到期日前不能记expired")
    if target not in {"expired", "invalidated"} and iso_date(card["as_of"]) >= iso_date(c["expiry_date"]) and c["state"] != "response_observed":
        raise ResearchError("等待期限已到，只能登记expired或invalidated，不能延长到成功")
    payload = {"candidate_id": candidate_id, "card_id": card_rec["record_id"],
               "previous_card_id": prior_card_id,
               "snapshot_id": card["snapshot_id"], "new_bar_evidence_id": fresh_bar["evidence_id"],
               "previous_state": c["state"], "new_state": target, "symbol": c["symbol"],
               "event_family_id": c["event_family_id"], "structure_revision_id": c["structure_revision_id"],
               "rule_hash": c["rule_hash"], "event_date": card["last_bar_date"],
               "decision_at": card["decision_at"], "confirmed_at": card["decision_at"],
               "last_dependency_at": card["last_dependency_at"],
               "evidence_ids": card["manual"]["supports"], "record_mode": c["record_mode"],
               "reason": require_text(request.get("reason"), "reason"),
               "semantic_status": "human_assessed_not_automatically_confirmed"}
    return store.append("candidate_event", payload)


def record_risk(store, request):
    card_rec = store.get(request.get("card_id"), "card")
    card = card_rec["payload"]
    return store.append("risk", {"card_id": card_rec["record_id"], "symbol": card["symbol"],
                       "decision_at": card["decision_at"], "risk_state": "observed",
                       "risk_type": require_text(request.get("risk_type"), "risk_type"),
                       "reason": require_text(request.get("reason"), "reason"),
                       "evidence_ids": card["manual"]["supports"],
                       "inventory_status": "unknown", "execution_status": "not_simulated",
                       "record_mode": card["record_mode"]})


def gate(store):
    snapshots = store.records("snapshot")
    market_coverages = []
    for record in snapshots:
        origin = record["payload"].get("origin")
        coverage = origin.get("market_coverage") if isinstance(origin, dict) else None
        if isinstance(coverage, dict):
            market_coverages.append(coverage)
    has_market_snapshot = bool(market_coverages)
    has_three_market_snapshot = any(c.get("sse_szse_bse_package_present") is True
                                    for c in market_coverages)
    blockers = ["真实来源/用途验收未完成", "历史PIT、公司行动、日历、证券状态/历史股票池未验收",
                "W-BASE及两个场景的可执行定义未冻结验证", "订单、T+1批次库存及历史费用模型未实现",
                "试验预算、成熟标签与样本外切分未预登记", "六道独立验证全部not_run",
                "全市场单日方向统计不含多日结构；自动候选与短中期策略输出仍被阻断"]
    if not snapshots:
        blockers.insert(0, "没有导入数据快照")
    return {"descriptive_workflow": "AVAILABLE" if snapshots else "NEEDS_DATA",
            "full_market_observation": ("PACKAGE_OBSERVED_UNCERTIFIED" if has_three_market_snapshot else
                                         "PARTIAL_SCOPE_OR_NEEDS_SNAPSHOT" if has_market_snapshot else "NEEDS_MARKET_SNAPSHOT"),
            "automated_candidate_scan": "BLOCK", "conditional_strategy_output": "BLOCK",
            "backtest_gate": "BLOCK", "result_validity": "not_run",
            "prospective_shadow": "BLOCK", "production_permission": False,
            "automatic_orders_enabled": False, "blockers": blockers,
            "note": "本机功能可用不等于策略通过验证。当前没有绩效统计或交易接口。"}
