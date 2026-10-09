"""Frozen Wyckoff scenario predicates and candidate state machines.

The scanner composes the structural primitives from :mod:`wyckoff_cn.wyckoff`
into the three pre-registered scenarios — W-BASE, W-SPRING-TEST and W-LPS-BU.
For every symbol it reports, per scenario, the conditions already matched (with
evidence), the conditions still missing, the current frozen state and any
structural invalidation. It never ranks symbols by an unvalidated score, never
emits a buy/sell signal and never registers an actionable candidate — the output
is descriptive research for human review, matching the design's "先过滤数据不合格
证券，再输出状态与证据；不以未验证分数强行排先后".
"""
from .common import ResearchError
from .wyckoff import DEFAULT_RULES, PATH_AMBIGUOUS, analyze, rules_fingerprint

SCENARIO_RULES_VERSION = "wyckoff-scenarios-1"

# Each scenario's ordered conditions reference Wyckoff event types. ``states[i]``
# is the state after i conditions have matched. ``resolution`` lists event types
# that advance past the last condition to a terminal-but-not-invalid state.
# Invalidation is a structural break (a decisive close through the stop level),
# computed separately. Windows/exit ordering are not yet registered, so no expiry.
SCENARIOS = {
    "W-BASE": {
        "description": "吸筹区间：区间形成后出现承接（spring或test），等待突破。",
        "sequence": [{"name": "absorption", "types": {"spring", "test"}}],
        "states": ["base_detected", "absorption_observed"],
        "resolution": {"sign_of_strength": "breakup_observed"},
        "requires_range": True,
        "requires_consolidation": True,
        "stop": "support",
    },
    "W-SPRING-TEST": {
        "description": "弹簧-测试：区间内弹簧震仓后回踩不破支撑，等待强势确认。",
        "sequence": [{"name": "spring", "types": {"spring"}},
                     {"name": "test", "types": {"test"}}],
        "states": ["base_forming", "spring_observed", "test_observed"],
        "resolution": {"sign_of_strength": "response_observed"},
        "requires_range": True,
        "requires_consolidation": True,
        "stop": "support",
    },
    "W-LPS-BU": {
        "description": "突破回踩：突破后最后支撑点守住原压力转支撑，等待延续。",
        "sequence": [{"name": "breakup", "types": {"sign_of_strength"}},
                     {"name": "lps", "types": {"last_point_of_support"}}],
        "states": ["awaiting_breakup", "breakup_observed", "lps_observed"],
        "resolution": {"sign_of_strength": "continuation_observed"},
        "requires_range": True,
        "requires_consolidation": False,
        "stop": "broken_resistance",
    },
}

NOT_MET = "not_met"
STRUCTURE_UNKNOWN = "structure_unknown"
INVALIDATED = "invalidated"


def scenario_fingerprint():
    return {"version": SCENARIO_RULES_VERSION, "rules": sorted(SCENARIOS), }


def _match_sequence(events, sequence):
    """Greedy in-order match; each condition takes the earliest unused event."""
    matched, cursor, used = [], -1, set()
    for cond in sequence:
        found = None
        for ev in events:
            if ev["id"] in used or ev["bar_index"] <= cursor:
                continue
            if ev["type"] in cond["types"] and ev["status"] != PATH_AMBIGUOUS:
                found = ev
                break
        matched.append({"condition": cond["name"], "event": found})
        if found:
            cursor = found["bar_index"]
            used.add(found["id"])
    return matched


def _resolution_event(events, resolution_types, after_index):
    for ev in events:
        if ev["bar_index"] > after_index and ev["type"] in resolution_types and ev["status"] != PATH_AMBIGUOUS:
            return ev
    return None


def _invalidation(symbol, bars, scenario, structure, matched):
    """Structural break: a decisive close through the scenario stop level."""
    rng = structure["range"]
    if rng is None:
        return None
    spec = SCENARIOS[scenario]
    if spec["stop"] == "support":
        stop_level, stop_basis = rng["support"], "range_support"
    else:  # broken_resistance: the level the break-up left behind
        breakup = next((m["event"] for m in matched if m["condition"] in {"breakup", "lps"} and m["event"]), None)
        stop_level = breakup["broken_level"] if breakup and "broken_level" in breakup else rng["resistance"]
        stop_basis = "broken_resistance"
    last_condition = max((m["event"]["bar_index"] for m in matched if m["event"]), default=-1)
    for i, bar in enumerate(bars):
        if i <= last_condition:
            continue
        if bar["close"] < stop_level:
            return {"triggered": True, "reason": "close_below_" + stop_basis,
                    "stop_level": stop_level, "bar_index": i, "date": bar["date"]}
    return {"triggered": False, "reason": None, "stop_level": stop_level, "stop_basis": stop_basis}


def evaluate(symbol, bars, as_of=None, rules=None):
    """Evaluate all three scenarios for one symbol's observed bars (pure)."""
    structure = analyze(symbol, bars, as_of, rules)
    events = structure["events"]
    rng = structure["range"]
    trend = structure["trend"]
    consolidation = trend["state"] == "consolidation"
    candidates = {}
    for scenario, spec in SCENARIOS.items():
        if spec["requires_range"] and rng is None:
            candidates[scenario] = _result(symbol, scenario, STRUCTURE_UNKNOWN, [], [],
                                           None, "区间边界未确认，无法判定场景")
            continue
        if spec["requires_consolidation"] and not consolidation:
            candidates[scenario] = _result(symbol, scenario, NOT_MET, [], [],
                                           None, "当前结构趋势非整理区间（" + trend["state"] + "）")
            continue
        matched = _match_sequence(events, spec["sequence"])
        matched_conditions = [{"condition": m["condition"], "event_id": m["event"]["id"],
                               "date": m["event"]["date"], "status": m["event"]["status"]}
                              for m in matched if m["event"]]
        missing = [m["condition"] for m in matched if not m["event"]]
        progress = len(matched_conditions)
        invalidation = _invalidation(symbol, bars, scenario, structure, matched)
        if invalidation and invalidation["triggered"]:
            state = INVALIDATED
        else:
            state = spec["states"][progress]
            last_index = max((m["event"]["bar_index"] for m in matched if m["event"]), default=-1)
            resolution = _resolution_event(events, spec["resolution"], last_index) if not missing else None
            if resolution:
                state = spec["resolution"][resolution["type"]]
                matched_conditions = matched_conditions + [{"condition": resolution["type"],
                                                            "event_id": resolution["id"],
                                                            "date": resolution["date"],
                                                            "status": resolution["status"]}]
        evidence = sorted({m["event"]["evidence_id"] for m in matched if m["event"]})
        status = "candidate_unvalidated" if matched_conditions else NOT_MET
        result = _result(symbol, scenario, state, matched_conditions, missing, invalidation, None)
        result["evidence_ids"] = evidence
        result["status"] = status
        candidates[scenario] = result
    return {"symbol": symbol, "analysis_type": "descriptive_wyckoff_scenario_scan",
            "as_of": structure["as_of"], "structure_rule_version": structure["rule_version"],
            "structure_rule_hash": structure["rule_hash"],
            "scenario_rule_version": SCENARIO_RULES_VERSION,
            "support": rng["support"] if rng else None,
            "resistance": rng["resistance"] if rng else None,
            "trend_state": trend["state"],
            "scenarios": candidates,
            "status": "descriptive_only", "auto_signal": "BLOCKED",
            "candidate_selection": "NOT_PERFORMED",
            "target_price": {"value": None, "status": "unknown_point_and_figure_measure_not_enabled"},
            "statement": "这是冻结威科夫场景谓词的描述性重算，不是选股结果、买卖信号或策略有效性结论。",
            "blockers": [
                "场景谓词、短/中期观察窗口与失效/退出顺序尚未完成预登记与实现复核。",
                "候选仅为未验证描述，未排序、未给出仓位、目标价或绩效。",
                "复权、公司行动、交易日历与证券状态未验收，结构边界可能失真。",
            ]}


def _result(symbol, scenario, state, matched, missing, invalidation, note):
    return {"symbol": symbol, "scenario": scenario,
            "scenario_rule_version": SCENARIO_RULES_VERSION,
            "state": state, "matched_conditions": matched,
            "missing_conditions": missing,
            "invalidation": invalidation, "note": note,
            # Every scenario result carries a validity status; the recompute path
            # upgrades it to candidate_unvalidated only when conditions matched.
            "status": NOT_MET,
            "observation_window": "not_registered", "expiry_status": "unknown_window_not_registered",
            "auto_signal": "BLOCKED", "production_permission": False}


def _qualified(bars, min_bars):
    """Data gate before scanning: enough complete bars, else unqualified."""
    complete = [b for b in bars if b.get("complete") == "true"]
    if len(complete) < min_bars:
        return None, "已声明完整的日线不足" + str(min_bars) + "根（" + str(len(complete)) + "）"
    return complete, None


def _bar_input(observed):
    return {"basis": observed["basis"], "bar_count": observed["bar_count"],
            "incomplete_bars_excluded": observed["incomplete_bars_excluded"],
            "price_basis": observed["price_basis"]}


def structure_symbol(store, request):
    """Frozen structural report for one symbol from retained observations (read-only)."""
    from . import history
    observed = history.observed_bars(store, request)
    report = analyze(observed["symbol"], observed["bars"], request.get("as_of"))
    report["bar_input"] = _bar_input(observed)
    return report


def evaluate_symbol(store, request):
    """Scenario evaluation for one symbol from retained observations (read-only)."""
    from . import history
    observed = history.observed_bars(store, request)
    bars, reason = _qualified(observed["bars"], DEFAULT_RULES["min_bars_for_structure"])
    if bars is None:
        raise ResearchError("数据不足，无法扫描: " + reason)
    result = evaluate(observed["symbol"], bars, request.get("as_of"))
    result["bar_input"] = _bar_input(observed)
    return result


def scan_market(store, request):
    """Scan the point-in-time universe; report candidates and filter reasons.

    ``include="candidates_only"`` trims the reported symbol list to those with at
    least one matched condition, and the omission is always counted explicitly —
    nothing is dropped silently and the trimmed view is never called complete.
    """
    from . import history
    request = request or {}
    include = request.get("include", "all")
    if include not in ("all", "candidates_only"):
        raise ResearchError("include只能是all或candidates_only")
    as_of = request.get("as_of")
    decision_at = request.get("decision_at")
    universe = history.universe_at(store, {"as_of": as_of, "decision_at": decision_at})
    bulk = history.symbol_bars(store, {"as_of": as_of, "decision_at": decision_at})
    min_bars = DEFAULT_RULES["min_bars_for_structure"]
    candidates, unqualified = [], []
    for member in universe["members"]:
        symbol = member["symbol"]
        entry = bulk["series"].get(symbol)
        if entry is None:
            unqualified.append({"symbol": symbol, "reason_code": "no_observed_rows",
                                "reason": "全市场历史库没有观察到该证券的价量行"})
            continue
        bars, reason = _qualified(entry["bars"], min_bars)
        if bars is None:
            unqualified.append({"symbol": symbol, "reason_code": "insufficient_complete_bars",
                                "reason": reason})
            continue
        try:
            candidates.append(evaluate(symbol, bars, as_of))
        except ResearchError as exc:
            unqualified.append({"symbol": symbol, "reason_code": "recompute_refused",
                                "reason": str(exc)})
    matched = [c for c in candidates
               if any(s["status"] == "candidate_unvalidated" for s in c["scenarios"].values())]
    reported = matched if include == "candidates_only" else candidates
    reason_counts = {}
    for entry in unqualified:
        reason_counts[entry["reason_code"]] = reason_counts.get(entry["reason_code"], 0) + 1
    return {"analysis_type": "descriptive_full_market_scenario_scan",
            "as_of": universe["as_of"], "snapshot_id": universe["snapshot_id"],
            "scenario_rule_version": SCENARIO_RULES_VERSION,
            "structure_rule_hash": rules_fingerprint(),
            "universe_basis": universe["universe_basis"],
            "member_count": universe["member_count"],
            "include": include,
            "scanned_count": len(candidates), "unqualified_count": len(unqualified),
            "candidates": reported,
            "candidates_omitted_not_matched": len(candidates) - len(reported),
            "unqualified": unqualified if include == "all" else [],
            "unqualified_list_omitted": 0 if include == "all" else len(unqualified),
            "unqualified_reason_counts": reason_counts,
            "candidate_count": len(matched),
            "status": "descriptive_only", "auto_signal": "BLOCKED",
            "ranked_by_score": False,
            "statement": "这是按当日有效证券池执行的冻结场景扫描；不是选股排名、买卖信号或策略有效性结论。",
            "blockers": [
                "场景规则、观察窗口与失效/退出顺序未预登记；候选只能描述，不能执行。",
                "未验证分数不参与排序；本输出不含先后名次。",
                "点时证券池、公司行动、复权与日历未验收，覆盖可能不完整。",
            ]}
