"""Frozen Wyckoff structural rules: swing points, ranges and event primitives.

This module is a pure, deterministic, recomputable rules engine. It never reads
the ledger, never selects candidates and never emits buy/sell signals. It turns
an ordered list of observed daily bars for one symbol into auditable structural
observations: confirmed swing pivots, the consolidation range envelope, and the
Wyckoff event primitives (spring, test, upthrust, sign-of-strength break-up and
last-point-of-support). Every observation carries evidence ids, the bars it
depends on, a rule version/hash and an explicit descriptive-only statement.

Point-in-time honesty: a swing pivot is only confirmed once the bars after it
exist, so each swing records both its pivot date and the later date that
confirmed it. When a required dependency is missing (no confirmed boundary, no
prior spring/test to compare, volume reference still warming up) the affected
observation is reported as ``unknown`` rather than invented. When a single bar
satisfies contradictory reversal primitives it is reported as
``path_ambiguous`` instead of being silently resolved.
"""
from .common import ResearchError, digest

RULES_VERSION = "wyckoff-structure-1"
DEFAULT_RULES = {
    "version": RULES_VERSION,
    "purpose": "descriptive_only",
    "technical_theory": "wyckoff_only",
    # Fractal half-window: a pivot needs `swing_lookback` bars on each side to be
    # confirmed, so its confirmation date is `swing_lookback` bars after the pivot.
    "swing_lookback": 2,
    # Trailing mean volume reference used for the dry-up/expansion observations.
    # The current bar is never part of its own reference (no centered windows).
    "volume_reference_window": 20,
    "current_bar_in_volume_reference": False,
    # Minimum observed bars before any structural label is attempted.
    "min_bars_for_structure": 5,
    # close_location (close position within the bar range) thresholds that decide
    # an up-close is "strong" (break-up) or a recovery is convincing. Frozen, not
    # fitted; changing them is a new rule version.
    "close_location_strong": 0.6,
    "close_location_recover": 0.5,
    "optimized": False,
    "performance_validation": "not_run",
}

SWING_HIGH = "swing_high"
SWING_LOW = "swing_low"
UNKNOWN = "unknown"
PATH_AMBIGUOUS = "path_ambiguous"
OBSERVED = "observed"


def rules_fingerprint(rules=None):
    return digest(rules or DEFAULT_RULES)


def bar_id(symbol, bar):
    """Stable structural evidence id for one observed bar."""
    return "bar-" + digest({"symbol": symbol, "snapshot_id": bar.get("snapshot_id"),
                            "date": bar["date"], "close": bar["close"]})[:24]


def _check_bars(symbol, bars):
    if not isinstance(symbol, str) or not symbol.strip():
        raise ResearchError("symbol不能为空")
    if not isinstance(bars, list) or not bars:
        raise ResearchError("结构分析需要非空的已观察日线序列")
    previous_date = None
    for bar in bars:
        if bar.get("complete") != "true":
            raise ResearchError("结构判定只接受已声明完整的日线；请先过滤不完整行情")
        if previous_date is not None and bar["date"] <= previous_date:
            raise ResearchError("日线日期必须严格递增；不静默排序或去重")
        for field in ("open", "high", "low", "close", "volume"):
            if not isinstance(bar.get(field), (int, float)):
                raise ResearchError("日线缺少数值字段: " + field)
        previous_date = bar["date"]
    return bars


def _facts(symbol, bars, rules):
    """Per-bar geometric/volume facts; trailing reference, never centered."""
    w = rules["volume_reference_window"]
    out = []
    for i, bar in enumerate(bars):
        spread = bar["high"] - bar["low"]
        reference_rows = bars[i - w:i] if i >= w else []
        mean_volume = sum(r["volume"] for r in reference_rows) / w if reference_rows else None
        missing = []
        if mean_volume is None:
            missing.append("volume_warmup")
        elif mean_volume == 0:
            missing.append("positive_volume_reference")
        out.append({
            "index": i, "date": bar["date"], "evidence_id": bar_id(symbol, bar),
            "range": spread,
            "close_location": (bar["close"] - bar["low"]) / spread if spread else None,
            "volume_reference": mean_volume,
            "relative_volume": (bar["volume"] / mean_volume) if mean_volume else None,
            "missing_dependencies": missing,
        })
    return out


def detect_swings(symbol, bars, rules=None):
    """Confirmed fractal swing pivots with PIT confirmation dates.

    A pivot must be the strict unique extremum of its ``2*lookback+1`` bar
    window, so plateaus are deliberately not declared as pivots (conservative).
    A pivot at index i is only confirmed once bars up to i+lookback exist.
    """
    rules = rules or DEFAULT_RULES
    _check_bars(symbol, bars)
    k = rules["swing_lookback"]
    swings = []
    n = len(bars)
    for i in range(k, n - k):
        window = bars[i - k:i + k + 1]
        center = bars[i]
        others = [b for j, b in enumerate(window) if j != k]
        if all(center["high"] > b["high"] for b in others):
            swings.append(_make_swing(symbol, SWING_HIGH, i, center["high"], bars, k))
        if all(center["low"] < b["low"] for b in others):
            swings.append(_make_swing(symbol, SWING_LOW, i, center["low"], bars, k))
    swings.sort(key=lambda s: s["index"])
    return swings


def _make_swing(symbol, kind, index, price, bars, k):
    pivot = bars[index]
    confirm = bars[index + k]
    return {
        "id": kind + "-" + digest({"symbol": symbol, "date": pivot["date"], "kind": kind})[:20],
        "type": kind, "symbol": symbol, "index": index,
        "date": pivot["date"], "price": price,
        "confirm_index": index + k, "confirm_date": confirm["date"],
        "evidence_id": bar_id(symbol, pivot),
        "dependency_ids": [bar_id(symbol, bars[j]) for j in range(index, index + k + 1)],
        "status": OBSERVED,
    }


def classify_trend(swings):
    """Trend leg from the last two swing highs and last two swing lows."""
    highs = [s for s in swings if s["type"] == SWING_HIGH]
    lows = [s for s in swings if s["type"] == SWING_LOW]
    base = {"state": UNKNOWN, "higher_high": None, "higher_low": None,
            "last_two_highs": [s["price"] for s in highs[-2:]],
            "last_two_lows": [s["price"] for s in lows[-2:]],
            "missing_dependencies": []}
    if len(highs) < 2:
        base["missing_dependencies"].append("two_swing_highs")
    if len(lows) < 2:
        base["missing_dependencies"].append("two_swing_lows")
    if base["missing_dependencies"]:
        return base
    hh = highs[-1]["price"] > highs[-2]["price"]
    hl = lows[-1]["price"] > lows[-2]["price"]
    lh = highs[-1]["price"] < highs[-2]["price"]
    ll = lows[-1]["price"] < lows[-2]["price"]
    base.update(higher_high=hh, higher_low=hl)
    if hh and hl:
        base["state"] = "uptrend"
    elif lh and ll:
        base["state"] = "downtrend"
    else:
        base["state"] = "consolidation"
        base["path_status"] = PATH_AMBIGUOUS if (hh and ll) or (lh and hl) else OBSERVED
    return base


def _visible_box(swings, index):
    """Range envelope from pivots confirmed strictly before `index`."""
    highs = [s for s in swings if s["type"] == SWING_HIGH and s["confirm_index"] < index]
    lows = [s for s in swings if s["type"] == SWING_LOW and s["confirm_index"] < index]
    if not highs or not lows:
        return None
    creek = max(highs, key=lambda s: s["price"])
    floor = min(lows, key=lambda s: s["price"])
    return {"resistance": creek["price"], "support": floor["price"],
            "resistance_anchor": creek, "support_anchor": floor}


def detect_events(symbol, bars, swings, rules=None):
    """Wyckoff event primitives relative to the confirmed range envelope.

    Two passes keep the primitives clean. Pass one walks the bars for boundary
    reversals and the first decisive break: ``spring``/``upthrust`` are
    pierce-and-fail reversals at the floor/creek, and ``sign_of_strength`` is the
    first close back above the creek (only the first close breaks the level; later
    closes above it are markup, not new break-ups). Pass two anchors the reaction
    lows — confirmed swing lows — to the prerequisites: ``test`` is a higher low
    held after a spring, ``last_point_of_support`` is a reaction low held above the
    broken creek. Missing prerequisites yield ``unknown``; a bar that triggers
    contradictory reversals is ``path_ambiguous``.
    """
    rules = rules or DEFAULT_RULES
    facts = _facts(symbol, bars, rules)
    strong = rules["close_location_strong"]
    recover = rules["close_location_recover"]
    springs, upthrusts, breaks = [], [], []
    creek_broken = False
    for i, fact in enumerate(facts):
        bar = bars[i]
        box = _visible_box(swings, i)
        floor = box["support"] if box else None
        creek = box["resistance"] if box else None
        cl = fact["close_location"]
        core_here = []

        # Spring: pierce the floor but close back at/above it (shakeout fails).
        if floor is not None and bar["low"] < floor and bar["close"] >= floor:
            status = UNKNOWN if cl is None else (OBSERVED if cl >= recover else PATH_AMBIGUOUS)
            ev = _event(symbol, "spring", i, fact, [box["support_anchor"]["evidence_id"]], status)
            if cl is None:
                ev["missing_dependencies"].append("close_location")
            ev.update(pierced_level=floor, spring_low=bar["low"], recovery_close_location=cl)
            springs.append(ev)
            core_here.append("spring")

        # Upthrust: pierce the creek but close back at/below it (fails).
        if creek is not None and bar["high"] > creek and bar["close"] <= creek:
            status = UNKNOWN if cl is None else (OBSERVED if cl <= (1 - recover) else PATH_AMBIGUOUS)
            ev = _event(symbol, "upthrust", i, fact, [box["resistance_anchor"]["evidence_id"]], status)
            if cl is None:
                ev["missing_dependencies"].append("close_location")
            ev.update(pierced_level=creek, failure_close_location=cl)
            upthrusts.append(ev)
            core_here.append("upthrust")

        # Sign of strength: first decisive close above the creek breaks the level.
        if creek is not None and bar["close"] > creek and not creek_broken:
            missing = []
            volume_status = UNKNOWN
            if fact["relative_volume"] is not None:
                volume_status = "expanded" if fact["relative_volume"] > 1 else "not_expanded"
            else:
                missing.append("volume_reference")
            strength_ok = cl is not None and cl >= strong
            ev = _event(symbol, "sign_of_strength", i, fact,
                        [box["resistance_anchor"]["evidence_id"]],
                        OBSERVED if strength_ok else PATH_AMBIGUOUS)
            ev["missing_dependencies"] = ev["missing_dependencies"] + missing
            ev.update(broken_level=creek, close_location=cl, volume_status=volume_status)
            breaks.append(ev)
            creek_broken = True
            core_here.append("sign_of_strength")

        # Contradictory reversals on one bar are not silently resolved.
        if len(core_here) > 1:
            for ev in springs + upthrusts + breaks:
                if ev["bar_index"] == i and ev["type"] in core_here:
                    ev["status"] = PATH_AMBIGUOUS
                    ev["path_note"] = "同根日线同时触发多个相反的边界原语"

    # Reaction lows (confirmed swing lows) carry the test / last-point-of-support.
    reactions = []
    for swing in swings:
        if swing["type"] != SWING_LOW:
            continue
        p = swing["index"]
        fact = facts[p]
        prior_spring = next((e for e in reversed(springs) if e["bar_index"] < p), None)
        prior_break = next((e for e in reversed(breaks) if e["bar_index"] < p), None)
        box = _visible_box(swings, swing["confirm_index"])
        floor = box["support"] if box else None

        if prior_spring is not None and swing["price"] >= prior_spring["spring_low"] and (
                floor is None or swing["price"] >= floor):
            ev = _event(symbol, "test", p, fact, [prior_spring["id"], swing["evidence_id"]], OBSERVED)
            if fact["relative_volume"] is None:
                ev["missing_dependencies"].append("volume_reference")
            ev.update(reference_spring_id=prior_spring["id"], test_low=swing["price"],
                      higher_low=True, compares_to_spring_low=prior_spring["spring_low"])
            reactions.append(ev)

        if prior_break is not None and swing["price"] >= prior_break["broken_level"]:
            ev = _event(symbol, "last_point_of_support", p, fact,
                        [prior_break["id"], swing["evidence_id"]], OBSERVED)
            ev.update(reference_level=prior_break["broken_level"], holds_above_broken_resistance=True)
            reactions.append(ev)

    return sorted(springs + upthrusts + breaks + reactions, key=lambda e: e["bar_index"])


def _event(symbol, kind, index, fact, extra_deps, status):
    return {
        "id": kind + "-" + digest({"symbol": symbol, "date": fact["date"], "kind": kind})[:20],
        "type": kind, "symbol": symbol, "bar_index": index, "date": fact["date"],
        "evidence_id": fact["evidence_id"],
        "dependency_ids": list(extra_deps),
        "rule_version": RULES_VERSION, "status": status,
        "missing_dependencies": list(fact["missing_dependencies"]),
    }


def analyze(symbol, bars, as_of=None, rules=None):
    """Assemble swings, trend, range envelope and event primitives.

    Pure and recomputable: same inputs and rule version always produce the same
    report. The output is descriptive research only — it is never a candidate,
    a buy/sell signal or a strategy-validity claim.
    """
    rules = rules or DEFAULT_RULES
    _check_bars(symbol, bars)
    if rules.get("purpose") != "descriptive_only" or rules.get("technical_theory") != "wyckoff_only":
        raise ResearchError("威科夫结构协议仅允许纯威科夫描述性事实")
    swings = detect_swings(symbol, bars, rules)
    trend = classify_trend(swings)
    box = _visible_box(swings, len(bars)) if bars else None
    events = detect_events(symbol, bars, swings, rules)
    missing = []
    if len(bars) < rules["min_bars_for_structure"]:
        missing.append("insufficient_bars_for_structure")
    if box is None:
        missing.append("no_confirmed_range_boundary")
    first, last = bars[0], bars[-1]
    return {
        "symbol": symbol,
        "analysis_type": "descriptive_wyckoff_structure",
        "rule_version": rules["version"], "rule_hash": rules_fingerprint(rules),
        "as_of": as_of or last["date"],
        "bar_span": [first["date"], last["date"]], "bar_count": len(bars),
        "snapshot_ids": sorted({b.get("snapshot_id") for b in bars if b.get("snapshot_id")}),
        "swings": swings, "swing_count": len(swings),
        "trend": trend,
        "range": (None if box is None else {
            "support": box["support"], "resistance": box["resistance"],
            "support_anchor_id": box["support_anchor"]["evidence_id"],
            "resistance_anchor_id": box["resistance_anchor"]["evidence_id"],
            "basis": "envelope_of_confirmed_swing_pivots_not_certified_wyckoff_range"}),
        "events": events, "event_count": len(events),
        "event_summary": _summarize(events),
        "missing_dependencies": sorted(set(missing)),
        "status": "descriptive_only",
        "automated_signal": "BLOCKED",
        "candidate_selection": "NOT_PERFORMED_BY_RULE_ENGINE",
        "target_price": {"value": None, "status": "unknown_point_and_figure_measure_not_enabled"},
        "blockers": [
            "威科夫区间边界、测试/响应与失效/退出顺序尚未通过分析者间一致性复核。",
            "摆动点与事件为冻结规则的描述性重算，不是买卖信号或已验证结构。",
            "复权、公司行动、交易日历和证券状态未验收；跨除权结构比较可能失真。",
            "候选场景W-BASE/W-LPS-BU/W-SPRING-TEST的组合由扫描层负责，本层不选股。",
        ],
        "statement": "这是冻结威科夫规则下的摆动点/区间/事件描述，不是投资建议、买卖点或策略有效性结论。",
    }


def _summarize(events):
    counts = {}
    for ev in events:
        key = ev["type"]
        slot = counts.setdefault(key, {"observed": 0, "unknown": 0, "path_ambiguous": 0})
        slot[ev["status"]] = slot.get(ev["status"], 0) + 1
    return counts
