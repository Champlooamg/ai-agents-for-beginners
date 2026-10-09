"""Conservative order-plan checks. Does not determine fills or report returns."""
from .common import ResearchError, SHANGHAI, instant, now, require_text
from .data import number


def order_guard(plan):
    reasons = []
    side = plan.get("side")
    if side not in {"buy", "sell"}:
        raise ResearchError("side必须为buy/sell")
    quantity = number(plan.get("quantity"), "quantity")
    price = number(plan.get("limit_price"), "limit_price")
    lot = number(plan.get("buy_lot_size"), "buy_lot_size")
    if quantity <= 0 or not quantity.is_integer() or price <= 0 or lot <= 0 or not lot.is_integer():
        raise ResearchError("数量/申报单位须为正整数，计划价须为正数")
    decision = instant(plan.get("decision_at"))
    earliest = instant(plan.get("earliest_order_at"))
    if earliest <= decision:
        reasons.append("订单最早时间必须严格晚于决策")
    if plan.get("calendar_verified") is not True:
        reasons.append("交易日历未验收")
    if plan.get("security_rules_verified") is not True:
        reasons.append("当时证券状态、价格限制和申报规则未验收")
    if plan.get("risk_budget_verified") is not True:
        reasons.append("风险预算未验收")
    if side == "buy":
        if int(quantity) % int(lot):
            reasons.append("买入数量不符合提供的申报单位")
        cash = number(plan.get("available_cash"), "available_cash", optional=True)
        fees = number(plan.get("estimated_fees"), "estimated_fees", optional=True)
        if cash is None or fees is None:
            reasons.append("可用资金或费用未知")
        elif cash < 0 or fees < 0 or quantity * price + fees > cash:
            reasons.append("费用/资金不合法或资金不足")
    else:
        sellable = 0.0
        reserved = number(plan.get("reserved_quantity", 0), "reserved_quantity")
        if reserved < 0:
            raise ResearchError("已冻结数量不得为负")
        lots = plan.get("inventory_lots")
        if not isinstance(lots, list):
            raise ResearchError("卖出必须提供批次库存inventory_lots")
        for batch in lots:
            bought = instant(batch.get("bought_at"))
            ready = instant(batch.get("sellable_from"))
            q = number(batch.get("remaining_quantity"), "remaining_quantity")
            if q < 0 or not q.is_integer():
                raise ResearchError("批次剩余数量须为非负整数")
            # Ordinary A-share T+1 constraint, without inventing the next session.
            if ready.astimezone(SHANGHAI).date() <= bought.astimezone(SHANGHAI).date():
                reasons.append("普通A股新仓不能当日转为可卖")
                continue
            if bought <= earliest and ready <= earliest:
                sellable += q
        if quantity > max(0, sellable - reserved):
            reasons.append("卖出超过批次合法可卖量减已冻结量")
    # Guard acceptance is only eligibility to review a plan, never a fill.
    return {"plan_constraints": "BLOCK" if reasons else "REVIEWABLE",
            "reasons": reasons, "fill_status": "unknown", "filled_quantity": None,
            "cash_debited": None, "realized_return": None,
            "automatic_orders_enabled": False, "order_mode": "paper_plan_only"}


def save_order_plan(store, request):
    card = store.get(request.get("card_id"), "card")["payload"]
    p = dict(request, decision_at=card["decision_at"], symbol=card["symbol"])
    p["rule_ref"] = require_text(p.get("rule_ref"), "rule_ref")
    result = order_guard(p)
    return store.append("order_plan", {"plan": p, "guard": result,
                                      "registered_at": now(), "record_mode": card["record_mode"]})
