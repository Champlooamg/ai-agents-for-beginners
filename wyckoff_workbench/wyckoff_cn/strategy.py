"""Conditional Wyckoff strategy cards — descriptive, never actionable.

A card turns one scanned scenario candidate into a recomputable conditional plan
that mirrors the design's minimum fields: scenario + rule version, observation
cutoff vs decision moment, the entry trigger predicate, a planned entry price
reference (structural boundary only), the structural invalidation rule, exit /
T+1 caveats, a research observation window, and risk fields. Target price and
position size stay unknown until the point-and-figure / range measure and the
risk model are adjudicated, frozen, pre-registered and independently validated.
The validity status is never raised above ``candidate_unvalidated`` /
``triggered_unvalidated`` here; only an out-of-sample-validated study may show
audited performance, and this module never produces one.
"""
from .common import ResearchError, digest, instant, iso_date, now
from .scan import INVALIDATED, NOT_MET, STRUCTURE_UNKNOWN

CARD_VERSION = "wyckoff-strategy-card-1"

# Frozen per-scenario conditional plan. Prices are structural references, not
# fitted targets; entry bands and profit targets require a measure that is not
# yet enabled, so they stay unknown.
SCENARIO_PLAN = {
    "W-BASE": {
        "entry_trigger": "收盘价向上突破区间阻力位（resistance）并以强势收盘确认突破；未突破前显示等待，触发价不等于成交价。",
        "entry_reference": "resistance",
        "invalidation_rule": "收盘价跌破区间支撑位（support）；盘中下破不作唯一依据，以收盘确认，跳空越过按当根收盘判定。",
        "stop_reference": "support",
    },
    "W-SPRING-TEST": {
        "entry_trigger": "弹簧震仓后测试回踩不破支撑，随后收盘强势确认（收盘高于测试高点）；未确认前显示等待。",
        "entry_reference": "support",
        "invalidation_rule": "收盘价跌破弹簧低点或区间支撑位（support）；以收盘确认，跳空越过按当根收盘判定。",
        "stop_reference": "support",
    },
    "W-LPS-BU": {
        "entry_trigger": "突破后最后支撑点（LPS）守住原阻力转支撑，随后收盘延续确认（收盘高于突破高点）；未确认前显示等待。",
        "entry_reference": "broken_resistance",
        "invalidation_rule": "收盘价跌破原阻力转支撑位（broken_resistance）；以收盘确认，跳空越过按当根收盘判定。",
        "stop_reference": "broken_resistance",
    },
}

TRIGGERED_STATES = {"breakup_observed", "response_observed", "continuation_observed"}
VALIDITY_TIERS = ["descriptive", "candidate_unvalidated", "triggered_unvalidated",
                  "out_of_sample_validated"]


def _reference_level(plan, scenario_result, evaluation):
    ref = plan["entry_reference"]
    if ref == "resistance":
        return evaluation.get("resistance")
    if ref == "support":
        return evaluation.get("support")
    # broken_resistance: the level the break-up left behind.
    invalidation = scenario_result.get("invalidation") or {}
    return invalidation.get("stop_level") or evaluation.get("resistance")


def build_card(evaluation, scenario, request):
    """Build one conditional card for a scanned scenario candidate (pure)."""
    if scenario not in evaluation.get("scenarios", {}):
        raise ResearchError("该扫描结果没有场景: " + str(scenario))
    result = evaluation["scenarios"][scenario]
    if result["state"] in (NOT_MET, STRUCTURE_UNKNOWN):
        raise ResearchError("场景" + scenario + "未形成候选（" + result["state"] + "），不能生成条件策略卡")
    plan = SCENARIO_PLAN[scenario]
    as_of = evaluation.get("as_of")
    decision = request.get("decision_at") or now()
    instant(decision)
    if as_of and iso_date(as_of) > instant(decision).astimezone(_shanghai()).date():
        raise ResearchError("决策时刻早于观察截止日，不允许补签")
    invalidated = result["state"] == INVALIDATED
    triggered = result["state"] in TRIGGERED_STATES
    reference = _reference_level(plan, result, evaluation)
    stop = (result.get("invalidation") or {}).get("stop_level")
    distance = (reference - stop) if (reference is not None and stop is not None and reference > stop) else None
    entry_status = "invalidated" if invalidated else (
        "confirmation_observed" if triggered else "waiting")
    return {
        "card_type": "conditional_strategy_card", "card_version": CARD_VERSION,
        "symbol": evaluation["symbol"], "scenario": scenario,
        "scenario_rule_version": evaluation["scenario_rule_version"],
        "structure_rule_version": evaluation.get("structure_rule_version"),
        "structure_rule_hash": evaluation.get("structure_rule_hash"),
        "observation_cutoff": as_of, "decision_moment": decision,
        "scenario_state": result["state"],
        "validity_status": ("triggered_unvalidated" if triggered else "candidate_unvalidated"),
        "validity_tier_index": VALIDITY_TIERS.index("triggered_unvalidated" if triggered else "candidate_unvalidated"),
        "performance_shown": False,
        "entry": {
            "trigger_predicate": plan["entry_trigger"],
            "confirmation_rule": "以当日收盘价确认；盘中触发不作为唯一依据。",
            "tradeable_next_step": "确认当根收盘后，于下一交易日按可成交假设评估；A股T+1，当日买入不可卖。",
            "status": entry_status,
            "planned_entry": {
                "reference_level": reference, "reference_basis": plan["entry_reference"],
                "price": reference, "band": None,
                "band_status": "unknown_requires_frozen_price_measure",
                "price_basis": "raw", "quote_granularity": "0.01",
                "limit_state": "unknown", "fill_risk": "not_simulated",
                "notes": ["触发价非成交价", "未复权原始价", "涨跌停与未成交风险未模拟"],
            },
        },
        "invalidation": {
            "rule": plan["invalidation_rule"], "stop_level": stop,
            "confirmation": "close_based", "gap_handling": "跳空越过按当根收盘判定，盘中不单独作为失效依据",
            "triggered": invalidated,
        },
        "exit": {
            "risk_condition": "W-WEAKNESS等预登记风险条件观察；本版未冻结具体阈值，只作结构退出。",
            "stop": "见失效点（结构失效位）", "structural_exit": "结构性退出；目标价/盈亏比未启用。",
            "t_plus_one": "A股T+1：当日买入当日不可卖；库存批次需显式跟踪。",
            "inventory": "unknown",
        },
        "holding_period": {
            "short_window": "not_registered", "mid_window": "not_registered",
            "unit": "trading_days", "label": "研究观察窗口（未做样本外评估，不能称预测或承诺）",
        },
        "risk": {
            "max_account_risk_budget": "unknown",
            "entry_to_stop_distance": distance,
            "distance_status": "computed_from_raw_structural_levels" if distance is not None else "unknown_levels_overlap_or_missing",
            "fees": "unknown", "slippage": "unknown", "fill": "not_simulated",
            "position_size": None,
            "position_size_status": "withheld_until_all_dependencies_known",
        },
        "target_price": {"value": None,
                         "status": "unknown_point_and_figure_measure_not_adjudicated"},
        "profit_ratio": {"value": None, "status": "unknown_requires_target"},
        "matched_conditions": result.get("matched_conditions", []),
        "missing_conditions": result.get("missing_conditions", []),
        "evidence_ids": result.get("evidence_ids", []),
        "observation_window_status": result.get("observation_window", "not_registered"),
        "production_permission": False, "automatic_orders": False,
        "status": "descriptive_only",
        "blockers": [
            "场景规则、短/中期观察窗口与失效/退出顺序尚未完成预登记与实现复核。",
            "目标价/盈亏比未做点数图/区间测量的理论裁决与验证，保持unknown。",
            "费用、滑点、成交与T+1库存模型未实现；任一依赖unknown即不给仓位数。",
            "六道独立验证全部not_run；有效性只到未验证候选/触发，不能展示绩效。",
        ],
        "statement": "这是可复算的条件研究计划卡，不是投资建议、买卖指令、仓位建议或已验证策略；有效性未经确认。",
    }


def _shanghai():
    from .common import SHANGHAI
    return SHANGHAI


def card_fingerprint(card):
    return digest({"symbol": card["symbol"], "scenario": card["scenario"],
                   "decision_moment": card["decision_moment"],
                   "structure_rule_hash": card.get("structure_rule_hash"),
                   "entry": card["entry"]["trigger_predicate"]})


def freeze_card(store, card):
    """Persist a built card immutably; server timestamps the ledger write."""
    if card.get("production_permission") is not False:
        raise ResearchError("条件策略卡禁止开启生产权限")
    if card.get("target_price", {}).get("value") is not None:
        raise ResearchError("未验证前禁止给出目标价")
    if card.get("risk", {}).get("position_size") is not None:
        raise ResearchError("依赖未明前禁止给出仓位数")
    payload = dict(card, card_fingerprint=card_fingerprint(card),
                   frozen_at=now(), interpretation_status="conditional_unvalidated")
    return store.append("strategy_card", payload)


def build_and_freeze(store, request):
    """Build one card from retained observations and freeze it, or preview it.

    ``preview`` returns the recomputable card without touching the ledger so the
    researcher can review the conditional plan first. Freezing is always an
    explicit request; the preview path is never a hidden write.
    """
    from .scan import evaluate_symbol
    request = request or {}
    evaluation = evaluate_symbol(store, request)
    card = build_card(evaluation, request.get("scenario"), request)
    if request.get("preview"):
        return {"preview": True, "frozen": False, "evaluation": _scan_digest(evaluation), "card": card}
    record = freeze_card(store, card)
    return {"preview": False, "frozen": True, "record_id": record["record_id"],
            "record_hash": record["record_hash"],
            "evaluation": _scan_digest(evaluation), "card": card}


def _scan_digest(evaluation):
    return {"symbol": evaluation["symbol"], "as_of": evaluation["as_of"],
            "scenario_rule_version": evaluation["scenario_rule_version"],
            "structure_rule_hash": evaluation.get("structure_rule_hash"),
            "trend_state": evaluation.get("trend_state"),
            "support": evaluation.get("support"), "resistance": evaluation.get("resistance"),
            "bar_input": evaluation.get("bar_input")}
