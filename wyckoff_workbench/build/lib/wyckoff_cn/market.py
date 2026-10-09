"""Read-only descriptive summaries for immutable full-market observations."""

import csv
import io

from .common import ResearchError


def market_snapshot_errors(payload, retained_source_refs=None):
    """Check internal consistency of the importer-owned market metadata."""
    origin = payload.get("origin")
    origin = origin if isinstance(origin, dict) else {}
    coverage = origin.get("market_coverage")
    if not isinstance(coverage, dict):
        return []
    errors = []
    rows = payload.get("rows", [])
    unpriced = origin.get("unpriced_a_shares")
    names = origin.get("security_names")
    expected_sha = origin.get("provider_response_sha256")
    if coverage.get("provider") != "tdx_daily_package":
        errors.append("来源适配器标识不符")
    if retained_source_refs is not None and (expected_sha, payload.get("source_id")) not in retained_source_refs:
        errors.append("全市场快照未关联到已留档的原始来源响应")
    if not isinstance(unpriced, list) or not isinstance(names, dict):
        errors.append("包内证券名单或名称映射缺失")
        unpriced, names = [], {}
    symbols = [row.get("symbol") for row in rows]
    unpriced_symbols = [item.get("symbol") for item in unpriced if isinstance(item, dict)]
    if len(symbols) != len(set(symbols)):
        errors.append("有价A股证券代码重复")
    if len(unpriced_symbols) != len(set(unpriced_symbols)):
        errors.append("无价A股证券代码重复")
    if set(symbols) & set(unpriced_symbols):
        errors.append("同一证券同时列为有价和无价")
    if any(not isinstance(symbol, str) or len(symbol) < 8 or symbol[:2] not in {"sh", "sz", "bj"}
           for symbol in symbols + unpriced_symbols):
        errors.append("市场快照包含无法识别的A股代码")
    if set(symbols + unpriced_symbols) - set(names):
        errors.append("市场快照有证券缺少名称映射")
    if any(not isinstance(names.get(symbol), str) or not names[symbol].strip()
           for symbol in symbols + unpriced_symbols if symbol in names):
        errors.append("市场快照证券名称映射无效")
    if any(row.get("date") != coverage.get("date") for row in rows):
        errors.append("行情行日期与覆盖日期不一致")
    if len(rows) != coverage.get("priced_a_share_count"):
        errors.append("有价行数与覆盖计数不一致")
    if len(unpriced_symbols) != coverage.get("unpriced_a_share_count"):
        errors.append("无价代码数与覆盖计数不一致")
    if len(rows) + len(unpriced_symbols) != coverage.get("a_share_code_count"):
        errors.append("代码总数与有价/无价分项不一致")
    counts = coverage.get("market_counts")
    if not isinstance(counts, dict):
        errors.append("分市场覆盖计数缺失")
    else:
        reported_scope = []
        reported_a_share_total = 0
        for market in ("sh", "sz", "bj"):
            rows_for_market = sum(symbol.startswith(market) for symbol in symbols)
            unpriced_for_market = sum(symbol.startswith(market) for symbol in unpriced_symbols)
            reported = counts.get(market)
            if not isinstance(reported, dict):
                errors.append(market + "市场计数缺失")
                continue
            if rows_for_market != reported.get("priced_a_shares") or unpriced_for_market != reported.get("unpriced_a_shares"):
                errors.append(market + "市场有价/无价代码计数不一致")
            if reported.get("package_present"):
                reported_scope.append(market)
            reported_a_share_total += (reported.get("a_share_codes") or 0)
            if (reported.get("a_share_codes") != rows_for_market + unpriced_for_market):
                errors.append(market + "市场普通A股代码总数不一致")
        if coverage.get("market_scope") != reported_scope:
            errors.append("市场范围与分市场行情文件状态不一致")
        if reported_a_share_total != len(rows) + len(unpriced_symbols):
            errors.append("分市场普通A股代码总数与全市场计数不一致")
    expected_all = coverage.get("market_scope") == ["sh", "sz", "bj"]
    if coverage.get("sse_szse_bse_package_present") is not expected_all:
        errors.append("沪深北覆盖标记与包内文件范围不一致")
    return errors


def market_overview(store, request):
    snapshot_id = request.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ResearchError("snapshot_id不能为空")
    snapshot = store.get(snapshot_id, "snapshot")
    payload = snapshot["payload"]
    origin = payload.get("origin")
    coverage = origin.get("market_coverage") if isinstance(origin, dict) else None
    if not isinstance(coverage, dict) or coverage.get("provider") != "tdx_daily_package":
        raise ResearchError("所选快照不是全市场TDX收盘包")
    retained_refs = {(record["payload"].get("source_sha256"), record["payload"].get("source_id"))
                     for record in store.records("source_capture")}
    errors = market_snapshot_errors(payload, retained_refs)
    if errors:
        raise ResearchError("全市场快照内部审计失败: " + "; ".join(errors))
    rows = payload["rows"]
    dates = {row["date"] for row in rows}
    if len(dates) != 1:
        raise ResearchError("全市场快照必须只包含一个交易日期")
    as_of = next(iter(dates))
    exchange = {key: {"observed_priced": 0, "up": 0, "down": 0,
                      "unchanged": 0, "missing_previous_close": 0,
                      "observed_amount_yuan": 0.0, "amount_rows": 0}
                for key in ("sh", "sz", "bj")}
    totals = {key: 0 for key in ("up", "down", "unchanged", "missing_previous_close")}
    amount_sum = 0.0
    amount_rows = 0
    for row in rows:
        market = row["symbol"][:2]
        if market not in exchange:
            raise ResearchError("全市场快照含无法识别的交易所代码")
        group = exchange[market]
        group["observed_priced"] += 1
        previous = row.get("previous_close")
        if previous is None or previous <= 0:
            group["missing_previous_close"] += 1
            totals["missing_previous_close"] += 1
        else:
            if row["close"] > previous:
                direction = "up"
            elif row["close"] < previous:
                direction = "down"
            else:
                direction = "unchanged"
            group[direction] += 1
            totals[direction] += 1
        if row["amount"] is not None:
            group["observed_amount_yuan"] += row["amount"]
            group["amount_rows"] += 1
            amount_sum += row["amount"]
            amount_rows += 1
    blockers = [
        "该快照只有一个收盘日，无法识别区间、测试、响应或多周期威科夫结构。",
        "代码表来自行情包，证券状态、ST/退市历史及全市场成员完整性未由交易所独立核验。",
        "价格复权、公司行动、交易日历和涨跌幅限制口径未验收。",
        "W-BASE、W-LPS-BU、W-SPRING-TEST的可执行谓词及短中期退出/持有期限仍未冻结并验证。",
    ]
    return {
        "snapshot_id": snapshot_id,
        "as_of": as_of,
        "source_id": payload["source_id"],
        "source_sha256": payload["source_sha256"],
        "analysis_type": "descriptive_full_market_close_observation",
        "market_scope": coverage.get("market_scope", []),
        "sse_szse_bse_package_present": coverage.get("sse_szse_bse_package_present", False),
        "universe": {
            "ordinary_a_share_codes_in_package": coverage.get("a_share_code_count"),
            "priced_rows_imported": len(rows),
            "unpriced_a_shares": coverage.get("unpriced_a_share_count"),
            "coverage_claim": "provider package parsed; exchange-level completeness not certified",
            "security_status": "unknown",
        },
        "one_day_direction_counts": totals,
        "by_exchange": exchange,
        "observed_amount_sum_yuan": amount_sum if amount_rows == len(rows) else None,
        "amount_rows_present": amount_rows,
        "status": "descriptive_only",
        "automated_candidate_selection": "BLOCKED",
        "candidate_count": None,
        "strategy_cards_generated": 0,
        "strategy_status": "BLOCKED_RULES_DATA_AND_VALIDATION",
        "blockers": blockers,
        "statement": "这是单日全市场价量观察，不是投资标的推荐、买卖信号或已验证策略。",
    }


def market_observation_csv(store, request):
    snapshot_id = request.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ResearchError("snapshot_id不能为空")
    snapshot = store.get(snapshot_id, "snapshot")
    payload = snapshot["payload"]
    origin = payload.get("origin")
    coverage = origin.get("market_coverage") if isinstance(origin, dict) else None
    if not isinstance(coverage, dict) or coverage.get("provider") != "tdx_daily_package":
        raise ResearchError("所选快照不是全市场TDX收盘包")
    retained_refs = {(record["payload"].get("source_sha256"), record["payload"].get("source_id"))
                     for record in store.records("source_capture")}
    errors = market_snapshot_errors(payload, retained_refs)
    if errors:
        raise ResearchError("全市场快照内部审计失败: " + "; ".join(errors))
    origin = payload["origin"]
    names = origin["security_names"]
    fields = ["symbol", "name", "date", "open", "high", "low", "close", "previous_close",
              "net_progress", "volume", "volume_unit", "amount", "amount_unit", "status", "observation_status"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in sorted(payload["rows"], key=lambda value: value["symbol"]):
        previous = row.get("previous_close")
        writer.writerow({"symbol": row["symbol"], "name": names[row["symbol"]], "date": row["date"],
            "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"],
            "previous_close": previous,
            "net_progress": row["close"] / previous - 1 if previous and previous > 0 else "",
            "volume": row["volume"], "volume_unit": payload["volume_unit"],
            "amount": row["amount"], "amount_unit": payload["amount_unit"],
            "status": row["status"], "observation_status": "priced_bar_observed"})
    for item in sorted(origin["unpriced_a_shares"], key=lambda value: value["symbol"]):
        writer.writerow({"symbol": item["symbol"], "name": names[item["symbol"]],
            "date": coverage["date"], "volume_unit": payload["volume_unit"],
            "amount_unit": payload["amount_unit"], "status": item.get("status", "unknown"),
            "observation_status": "no_positive_close; trading_status_unknown"})
    return output.getvalue()
