"""Point-in-time read models over immutable full-market snapshots.

The multi-day history library, the point-in-time observed universe and the
trading calendar are derived views over full-market snapshots already retained
in the ledger. They never rewrite snapshots, never combine different sources
and never infer listing, delisting, suspension, holiday or corporate-action
causes that the source did not state. Days without a retained snapshot are not
evidence of a non-trading day.
"""
from .common import SHANGHAI, ResearchError, instant, iso_date, now
from .market import market_snapshot_errors

PRICE_EPSILON = 1e-6
UNKNOWN_UNIVERSE_ATTRIBUTES = ["listing_dates", "delisting_dates", "board", "st_status",
    "suspension_status", "price_limit_rules", "corporate_actions", "adjustment_factors"]
HISTORY_BLOCKERS = [
    "证券池只反映包内观察到的代码，上市/退市、ST、停复牌、板块和涨跌幅限制属性未提供。",
    "交易日历只由已留存快照日期构成，不是交易所官方日历；缺快照日期的交易状态未知。",
    "公司行动与复权未对账：previous_close差异只作观察标记，不自动复权、不推断原因。",
    "来源完整性和历史PIT均未认证；这是描述性研究视图，不是投资建议或策略有效性结论。",
]


def _retained_refs(store):
    return {(record["payload"].get("source_sha256"), record["payload"].get("source_id"))
            for record in store.records("source_capture")}


def market_snapshot_index(store, decision=None):
    """Return ordered (day, record, coverage) tuples for usable market snapshots.

    Refuses mixed sources and duplicate observed dates instead of silently
    choosing or merging; refuses snapshots captured after the decision moment.
    """
    decision = decision or now()
    instant(decision)
    refs = _retained_refs(store)
    entries = {}
    source_ids = set()
    for record in store.records("snapshot"):
        payload = record["payload"]
        origin = payload.get("origin")
        coverage = origin.get("market_coverage") if isinstance(origin, dict) else None
        if not isinstance(coverage, dict):
            continue
        errors = market_snapshot_errors(payload, refs)
        if errors:
            raise ResearchError("全市场快照内部审计失败: " + "; ".join(errors))
        day = iso_date(coverage.get("date"), "覆盖日期")
        if day in entries:
            raise ResearchError("观察日期" + day.isoformat() + "有多个全市场快照；不静默选择或合并")
        entries[day] = (record, coverage)
        source_ids.add(payload.get("source_id"))
    if len(source_ids) > 1:
        raise ResearchError("全市场快照来自多个来源(" + ", ".join(sorted(map(str, source_ids))) +
                            ")；禁止拼接不同来源")
    ordered = [(day,) + entries[day] for day in sorted(entries)]
    for day, record, _coverage in ordered:
        payload = record["payload"]
        if max(instant(payload["captured_at"]), instant(payload["ingested_at"])) > instant(decision):
            raise ResearchError("决策时间早于" + day.isoformat() + "快照采集/本机导入时间，不允许补签")
    return ordered


def _window(entries, cutoff, start, end):
    selected = []
    for day, record, coverage in entries:
        if cutoff is not None and day > cutoff:
            continue
        if start is not None and day < start:
            continue
        if end is not None and day > end:
            continue
        selected.append((day, record, coverage))
    return selected


def _aggregate(entries):
    """Fold day snapshots into per-symbol states and priced bars."""
    symbols = {}
    for day, record, _coverage in entries:
        payload = record["payload"]
        origin = payload["origin"]
        names = origin["security_names"]
        stamp = day.isoformat()
        for row in payload["rows"]:
            item = symbols.setdefault(row["symbol"], {"name": names[row["symbol"]], "states": {}, "bars": []})
            item["states"][stamp] = "priced"
            item["bars"].append({"date": row["date"], "open": row["open"], "high": row["high"],
                "low": row["low"], "close": row["close"], "volume": row["volume"],
                "amount": row["amount"], "previous_close": row.get("previous_close"),
                "complete": row["complete"], "status": row["status"],
                "snapshot_id": record["record_id"]})
        for entry in origin["unpriced_a_shares"]:
            item = symbols.setdefault(entry["symbol"], {"name": names[entry["symbol"]], "states": {}, "bars": []})
            item["states"][stamp] = "unpriced"
        for entry in origin["no_ohlc_a_shares"]:
            item = symbols.setdefault(entry["symbol"], {"name": names[entry["symbol"]], "states": {}, "bars": []})
            item["states"][stamp] = "no_ohlc"
    for item in symbols.values():
        item["bars"].sort(key=lambda bar: bar["date"])
    return symbols


def _missing_bars(item, observed_days):
    priced_days = sorted(day for day, state in item["states"].items() if state == "priced")
    if len(priced_days) < 2:
        return []
    first, last = priced_days[0], priced_days[-1]
    return [{"date": day, "observed_state": item["states"].get(day, "absent_from_package")}
            for day in observed_days if first < day < last and item["states"].get(day) != "priced"]


def _corporate_action_annotations(store):
    """Observed source annotations indexed by symbol/date; never certified."""
    index = {}
    for record in store.records("snapshot"):
        origin = record["payload"].get("origin")
        actions = origin.get("corporate_actions") if isinstance(origin, dict) else None
        for entry in actions if isinstance(actions, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("symbol"), str) and isinstance(entry.get("date"), str):
                index.setdefault(entry["symbol"], {}).setdefault(entry["date"], []).append(
                    {"snapshot_id": record["record_id"], "note": entry.get("note")})
    return index


def _discrepancies(symbol, bars, annotations):
    out = []
    for prior, bar in zip(bars, bars[1:]):
        previous = bar["previous_close"]
        if previous is None or abs(previous - prior["close"]) <= PRICE_EPSILON:
            continue
        out.append({"symbol": symbol, "date": bar["date"], "prior_date": prior["date"],
            "previous_close": previous, "prior_close": prior["close"],
            "difference": previous - prior["close"],
            "interpretation": "possible_corporate_action_or_data_revision; not_adjusted",
            "source_annotations": annotations.get(symbol, {}).get(bar["date"], [])})
    return out


def _membership(symbol, item, observed_days, annotations):
    states = item["states"]
    observed = sorted(states)
    priced = [day for day in observed if states[day] == "priced"]
    return {"symbol": symbol, "name": item["name"], "status": "unknown",
        "first_observed": observed[0], "last_observed": observed[-1],
        "observed_days": len(observed),
        "priced_bars": len(priced),
        "unpriced_days": sum(1 for day in observed if states[day] == "unpriced"),
        "no_ohlc_days": sum(1 for day in observed if states[day] == "no_ohlc"),
        "bar_span": [priced[0], priced[-1]] if priced else None,
        "missing_bar_count": len(_missing_bars(item, observed_days)),
        "previous_close_discrepancy_count": len(_discrepancies(symbol, item["bars"], annotations))}


def trading_calendar(store, request=None):
    """Observed trading calendar: only dates with retained market snapshots."""
    request = request or {}
    decision = request.get("decision_at") or now()
    entries = market_snapshot_index(store, decision)
    days = [day for day, _record, _coverage in entries]
    observed = set(days)
    weekend_observations, unobserved_weekdays = [], []
    if days:
        cursor = days[0]
        while cursor <= days[-1]:
            if cursor in observed:
                if cursor.weekday() >= 5:
                    weekend_observations.append(cursor.isoformat())
            elif cursor.weekday() < 5:
                unobserved_weekdays.append(cursor.isoformat())
            cursor = cursor.fromordinal(cursor.toordinal() + 1)
    return {"observed_trading_days": [day.isoformat() for day in days],
        "observed_count": len(days),
        "observed_span": [days[0].isoformat(), days[-1].isoformat()] if days else [],
        "calendar_basis": "dates_with_retained_full_market_snapshots",
        "trading_day_status": "observed_dates_are_the_only_confirmed_trading_days",
        "non_trading_day_status": "unknown_not_inferred_from_missing_snapshots",
        "unobserved_weekdays": unobserved_weekdays,
        "unobserved_weekday_status": "not_observed_trading_status_unknown",
        "weekend_observations": weekend_observations,
        "weekend_observation_status": "anomaly_reported_not_corrected",
        "status": "descriptive_only",
        "statement": "这是已留存全市场快照日期的观察日历，不是交易所官方交易日历；未观察到快照的日期不能解释为非交易日。"}


def universe_at(store, request):
    """Point-in-time observed universe for one date.

    The universe is whatever that date's package contained; attributes the
    source does not carry stay unknown and symbols observed earlier but absent
    on this date are reported with cause unknown rather than dropped.
    """
    day = iso_date(request.get("as_of"), "观察截止日")
    decision = request.get("decision_at") or now()
    if day > instant(decision).astimezone(SHANGHAI).date():
        raise ResearchError("观察截止日不能晚于决策时的上海日期")
    entries = market_snapshot_index(store, decision)
    selected = _window(entries, cutoff=day, start=day, end=day)
    if not selected:
        raise ResearchError(day.isoformat() + "没有已留存的全市场快照；不能据此判断该日是否为交易日")
    history = _aggregate(_window(entries, cutoff=day, start=None, end=None))
    record, coverage = selected[-1][1], selected[-1][2]
    states = {"priced": "priced_bar_observed", "unpriced": "no_positive_close_in_package",
              "no_ohlc": "reference_close_only_no_ohlc_in_package"}
    members, absent = [], []
    for symbol, item in sorted(history.items()):
        state = item["states"].get(day.isoformat())
        span = {"symbol": symbol, "name": item["name"],
                "first_observed": sorted(item["states"])[0], "last_observed": sorted(item["states"])[-1]}
        if state is None:
            absent.append({**span, "absence_status": "cause_unknown_not_inferred"})
        else:
            members.append({**span, "observed_state": state, "state_detail": states[state]})
    return {"as_of": day.isoformat(), "snapshot_id": record["record_id"],
        "source_id": record["payload"]["source_id"], "source_sha256": record["payload"]["source_sha256"],
        "analysis_type": "point_in_time_observed_universe",
        "universe_basis": "codes_present_in_that_dates_retained_package",
        "market_scope": coverage.get("market_scope", []),
        "members": members, "member_count": len(members),
        "previously_observed_absent_on_date": absent,
        "absent_count": len(absent),
        "unknown_universe_attributes": list(UNKNOWN_UNIVERSE_ATTRIBUTES),
        "status": "descriptive_only",
        "statement": "这是该日包内观察到的证券名单，不是交易所认证的当日有效股票池；缺名单不等于退市，出现名单不等于正常交易。"}


def series_for(store, request):
    """One symbol's multi-day priced series with quality observations."""
    symbol = request.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ResearchError("symbol不能为空")
    decision = request.get("decision_at") or now()
    cutoff = iso_date(request["as_of"], "观察截止日") if request.get("as_of") else None
    if cutoff is not None and cutoff > instant(decision).astimezone(SHANGHAI).date():
        raise ResearchError("观察截止日不能晚于决策时的上海日期")
    start = iso_date(request["window_start"], "窗口起始日") if request.get("window_start") else None
    end = iso_date(request["window_end"], "窗口结束日") if request.get("window_end") else None
    if start is not None and end is not None and start > end:
        raise ResearchError("窗口起始日不能晚于结束日")
    entries = _window(market_snapshot_index(store, decision), cutoff, start, end)
    if not entries:
        raise ResearchError("该观察窗口没有已留存的全市场快照")
    observed_days = [day.isoformat() for day, _r, _c in entries]
    symbols = _aggregate(entries)
    item = symbols.get(symbol)
    if item is None:
        raise ResearchError("该窗口的全市场快照没有观察到证券" + symbol + "；不能解释为该证券不存在")
    annotations = _corporate_action_annotations(store)
    return {"symbol": symbol, "name": item["name"], "analysis_type": "descriptive_symbol_history",
        "window": {"start": observed_days[0], "end": observed_days[-1],
                   "observed_trading_days": len(observed_days), "requested_cutoff": cutoff.isoformat() if cutoff else None},
        "bars": item["bars"], "bar_count": len(item["bars"]),
        "day_states": [{"date": day, "observed_state": state} for day, state in sorted(item["states"].items())],
        "missing_bars": _missing_bars(item, observed_days),
        "previous_close_discrepancies": _discrepancies(symbol, item["bars"], annotations),
        "units": "as_recorded_in_snapshot", "price_basis": "raw",
        "status": "descriptive_only",
        "blockers": list(HISTORY_BLOCKERS),
        "statement": "这是多日价量观察序列，不是威科夫结构判定、买卖信号或策略有效性结论。"}


def symbol_bars(store, request):
    """Priced bars for every observed symbol in one window, folded in a single pass.

    The full-market scenario scan needs each symbol's series; folding the day
    snapshots once keeps the scan linear in total rows instead of re-aggregating
    the whole market for every symbol. Read-only derived view.
    """
    request = request or {}
    decision = request.get("decision_at") or now()
    cutoff = iso_date(request["as_of"], "观察截止日") if request.get("as_of") else None
    if cutoff is not None and cutoff > instant(decision).astimezone(SHANGHAI).date():
        raise ResearchError("观察截止日不能晚于决策时的上海日期")
    start = iso_date(request["window_start"], "窗口起始日") if request.get("window_start") else None
    end = iso_date(request["window_end"], "窗口结束日") if request.get("window_end") else None
    if start is not None and end is not None and start > end:
        raise ResearchError("窗口起始日不能晚于结束日")
    entries = _window(market_snapshot_index(store, decision), cutoff, start, end)
    if not entries:
        raise ResearchError("该观察窗口没有已留存的全市场快照")
    observed_days = [day.isoformat() for day, _r, _c in entries]
    symbols = _aggregate(entries)
    return {"as_of": cutoff.isoformat() if cutoff else observed_days[-1],
        "window": {"start": observed_days[0], "end": observed_days[-1],
                   "observed_trading_days": len(observed_days)},
        "series": {symbol: {"name": item["name"], "bars": item["bars"]}
                   for symbol, item in sorted(symbols.items())},
        "status": "descriptive_only",
        "statement": "这是单次折叠得到的多证券价量序列映射，只供确定性重算使用；不是选股结果、买卖信号或策略有效性结论。"}


def observed_bars(store, request):
    """One symbol's declared-complete bars as structural recomputation input.

    Rows come from one retained snapshot or from the multi-day market history;
    the basis is always reported. Bars not declared complete are excluded from
    structural rules and counted explicitly — never silently dropped, and never
    interpreted as suspension, delisting or a missing trading day.
    """
    symbol = request.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ResearchError("symbol不能为空")
    decision = request.get("decision_at") or now()
    cutoff = iso_date(request["as_of"], "观察截止日") if request.get("as_of") else None
    if cutoff is not None and cutoff > instant(decision).astimezone(SHANGHAI).date():
        raise ResearchError("观察截止日不能晚于决策时的上海日期")
    if request.get("snapshot_id"):
        record = store.get(request["snapshot_id"], "snapshot")
        payload = record["payload"]
        if max(instant(payload["captured_at"]), instant(payload["ingested_at"])) > instant(decision):
            raise ResearchError("决策时间早于快照采集/本机导入时间，不允许补签")
        rows = [r for r in payload["rows"] if r["symbol"] == symbol
                and (cutoff is None or iso_date(r["date"], "行情日期") <= cutoff)]
        bars = [dict(r, snapshot_id=record["record_id"]) for r in rows]
        basis = {"basis": "retained_snapshot_rows", "snapshot_id": record["record_id"],
                 "source_id": payload["source_id"]}
    else:
        query = {"symbol": symbol, "decision_at": decision}
        if cutoff is not None:
            query["as_of"] = cutoff.isoformat()
        for key in ("window_start", "window_end"):
            if request.get(key):
                query[key] = request[key]
        series = series_for(store, query)
        bars = series["bars"]
        basis = {"basis": "multi_day_market_history", "window": series["window"]}
    if not bars:
        raise ResearchError("没有观察到证券" + symbol + "的行情行")
    complete = [b for b in bars if b.get("complete") == "true"]
    if not complete:
        raise ResearchError("证券" + symbol + "没有已声明完整的日线；结构判定不接受不完整行情")
    return {"symbol": symbol, "bars": complete, "bar_count": len(complete),
        "incomplete_bars_excluded": len(bars) - len(complete),
        "price_basis": "raw", **basis, "status": "descriptive_only",
        "blockers": list(HISTORY_BLOCKERS),
        "statement": "这是结构重算输入的观察日线；未声明完整的日线已排除并计数，不推断停牌、退市或缺根原因。"}


def market_history(store, request):
    """Cross-section summary of the multi-day market history library."""
    request = request or {}
    decision = request.get("decision_at") or now()
    cutoff = iso_date(request["as_of"], "观察截止日") if request.get("as_of") else None
    if cutoff is not None and cutoff > instant(decision).astimezone(SHANGHAI).date():
        raise ResearchError("观察截止日不能晚于决策时的上海日期")
    start = iso_date(request["window_start"], "窗口起始日") if request.get("window_start") else None
    end = iso_date(request["window_end"], "窗口结束日") if request.get("window_end") else None
    if start is not None and end is not None and start > end:
        raise ResearchError("窗口起始日不能晚于结束日")
    entries = _window(market_snapshot_index(store, decision), cutoff, start, end)
    if not entries:
        raise ResearchError("该观察窗口没有已留存的全市场快照")
    observed_days = [day.isoformat() for day, _r, _c in entries]
    symbols = _aggregate(entries)
    annotations = _corporate_action_annotations(store)
    membership = {}
    totals = {"priced_bars": 0, "unpriced_symbol_days": 0, "no_ohlc_symbol_days": 0,
              "missing_bars": 0, "previous_close_discrepancies": 0, "symbols_with_discrepancies": 0}
    for symbol, item in symbols.items():
        summary = _membership(symbol, item, observed_days, annotations)
        membership[symbol] = summary
        totals["priced_bars"] += summary["priced_bars"]
        totals["unpriced_symbol_days"] += summary["unpriced_days"]
        totals["no_ohlc_symbol_days"] += summary["no_ohlc_days"]
        totals["missing_bars"] += summary["missing_bar_count"]
        totals["previous_close_discrepancies"] += summary["previous_close_discrepancy_count"]
        totals["symbols_with_discrepancies"] += 1 if summary["previous_close_discrepancy_count"] else 0
    days = [{"date": day.isoformat(), "snapshot_id": record["record_id"],
             "source_id": record["payload"]["source_id"], "source_sha256": record["payload"]["source_sha256"],
             "priced_a_share_count": coverage.get("priced_a_share_count"),
             "unpriced_a_share_count": coverage.get("unpriced_a_share_count"),
             "no_ohlc_a_share_count": coverage.get("no_ohlc_a_share_count"),
             "a_share_code_count": coverage.get("a_share_code_count")}
            for day, record, coverage in entries]
    return {"analysis_type": "descriptive_multi_day_market_history",
        "window": {"start": observed_days[0], "end": observed_days[-1],
                   "observed_trading_days": len(observed_days)},
        "days": days, "symbol_count": len(membership),
        "membership": membership,
        "calendar": trading_calendar(store, {"decision_at": decision}),
        "quality": {"observed_market_days": len(observed_days), "duplicate_dates": 0,
                    "source_mixing": "none_single_source_checked",
                    "pit_gate": "snapshots_after_decision_time_refused",
                    **totals},
        "status": "descriptive_only",
        "blockers": list(HISTORY_BLOCKERS),
        "statement": "这是多日全市场历史库的描述性汇总，不是选股结果、买卖信号或策略有效性结论。"}
