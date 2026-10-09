import unittest
from datetime import date, timedelta

from wyckoff_cn.common import ResearchError
from wyckoff_cn.wyckoff import (DEFAULT_RULES, PATH_AMBIGUOUS, SWING_HIGH, SWING_LOW,
                                UNKNOWN, analyze, classify_trend, detect_events, detect_swings)


def mkbar(i, high, low, close, volume=100, complete="true"):
    return {"symbol": "sh600001", "date": (date(2026, 7, 1) + timedelta(days=i)).isoformat(),
            "open": (low + high) / 2, "high": high, "low": low, "close": close,
            "volume": volume, "amount": None, "previous_close": None,
            "complete": complete, "status": "unknown", "snapshot_id": "snap-%02d" % i}


def swing(kind, price, index=0, confirm_index=3, evidence_id="ev"):
    return {"id": "%s-%d" % (kind, index), "type": kind, "price": price, "index": index,
            "confirm_index": confirm_index, "evidence_id": evidence_id, "date": "d", "symbol": "sh600001"}


class SwingTests(unittest.TestCase):
    def test_detects_strict_fractal_pivots_with_pit_confirm_dates(self):
        bars = [mkbar(i, h, l, (h + l) / 2) for i, (h, l) in enumerate([
            (10, 9), (11, 9.5), (13, 11), (12, 9), (11, 8), (10, 8.5), (11, 9)])]
        swings = detect_swings("sh600001", bars)
        self.assertEqual([(s["type"], s["price"]) for s in swings],
                         [(SWING_HIGH, 13), (SWING_LOW, 8)])
        high = swings[0]
        self.assertEqual(high["index"], 2)
        self.assertEqual(high["confirm_index"], 4)
        self.assertEqual(high["date"], bars[2]["date"])
        self.assertEqual(high["confirm_date"], bars[4]["date"])
        low = swings[1]
        self.assertEqual(low["index"], 4)
        self.assertEqual(low["confirm_date"], bars[6]["date"])

    def test_plateau_is_not_a_pivot(self):
        # Two equal highs in the window: no strict unique extremum, so no pivot.
        bars = [mkbar(i, h, l, (h + l) / 2) for i, (h, l) in enumerate([
            (10, 9), (13, 9), (13, 9), (12, 9), (11, 9)])]
        self.assertEqual([s for s in detect_swings("sh600001", bars) if s["type"] == SWING_HIGH], [])

    def test_insufficient_bars_confirm_nothing(self):
        bars = [mkbar(i, 12 - i, 9 - i, 10 - i) for i in range(3)]
        self.assertEqual(detect_swings("sh600001", bars), [])


class TrendTests(unittest.TestCase):
    def test_trend_states(self):
        def t(highs, lows):
            return classify_trend([swing(SWING_HIGH, h) for h in highs] +
                                  [swing(SWING_LOW, l) for l in lows])["state"]
        self.assertEqual(t([10, 12], [8, 9]), "uptrend")
        self.assertEqual(t([12, 10], [9, 8]), "downtrend")
        self.assertEqual(t([10, 12], [9, 8]), "consolidation")

    def test_unknown_when_swings_missing(self):
        result = classify_trend([swing(SWING_HIGH, 10)])
        self.assertEqual(result["state"], UNKNOWN)
        self.assertIn("two_swing_lows", result["missing_dependencies"])

    def test_divergent_consolidation_is_path_ambiguous(self):
        result = classify_trend([swing(SWING_HIGH, 10), swing(SWING_HIGH, 12),
                                 swing(SWING_LOW, 9), swing(SWING_LOW, 8)])
        self.assertEqual(result["state"], "consolidation")
        self.assertEqual(result["path_status"], PATH_AMBIGUOUS)


class EventTests(unittest.TestCase):
    def box_swings(self):
        return [swing(SWING_LOW, 10.0, index=1, confirm_index=2, evidence_id="ev-floor"),
                swing(SWING_HIGH, 12.0, index=2, confirm_index=2, evidence_id="ev-creek")]

    def test_spring_test_break_and_lps_sequence(self):
        bars = [
            mkbar(0, 11, 10.5, 10.8), mkbar(1, 11, 10.4, 10.7),
            mkbar(2, 11.5, 10.5, 11.0), mkbar(3, 11.5, 10.6, 11.0),
            mkbar(4, 11, 9, 10.5),          # spring: pierce floor 10, close back above
            mkbar(5, 11, 10.2, 10.7),
            mkbar(6, 11, 10.3, 10.9),       # reaction low -> test
            mkbar(7, 12.6, 11.5, 12.4),     # sign of strength: close above creek 12
            mkbar(8, 13, 12.1, 12.7),       # reaction low -> last point of support
        ]
        swings = self.box_swings() + [
            swing(SWING_LOW, 10.3, index=6, confirm_index=8, evidence_id="ev-test"),
            swing(SWING_LOW, 12.1, index=8, confirm_index=10, evidence_id="ev-lps")]
        events = detect_events("sh600001", bars, swings)
        by_type = {e["type"]: e for e in events}
        self.assertEqual(set(by_type),
                         {"spring", "test", "sign_of_strength", "last_point_of_support"})
        self.assertEqual(by_type["spring"]["bar_index"], 4)
        self.assertEqual(by_type["spring"]["pierced_level"], 10.0)
        self.assertEqual(by_type["test"]["reference_spring_id"], by_type["spring"]["id"])
        self.assertTrue(by_type["test"]["higher_low"])
        self.assertEqual(by_type["sign_of_strength"]["bar_index"], 7)
        self.assertEqual(by_type["sign_of_strength"]["broken_level"], 12.0)
        # Volume reference warms up over 20 bars, so this short series stays unknown.
        self.assertEqual(by_type["sign_of_strength"]["volume_status"], UNKNOWN)
        self.assertIn("volume_reference", by_type["sign_of_strength"]["missing_dependencies"])
        self.assertEqual(by_type["last_point_of_support"]["reference_level"], 12.0)
        self.assertEqual(by_type["last_point_of_support"]["reference_level"],
                         by_type["sign_of_strength"]["broken_level"])

    def test_upthrust_and_contradictory_bar(self):
        bars = [mkbar(0, 11, 10.5, 10.8), mkbar(1, 11, 10.4, 10.7),
                mkbar(2, 11.5, 10.5, 11.0), mkbar(3, 12.5, 10.2, 10.4)]  # upthrust: pierce 12, close under
        swings = self.box_swings()
        events = detect_events("sh600001", bars, swings)
        up = [e for e in events if e["type"] == "upthrust"]
        self.assertEqual(len(up), 1)
        self.assertEqual(up[0]["bar_index"], 3)

        # One bar that both springs below the floor and closes above the creek.
        bars = [mkbar(0, 11, 10.5, 10.8), mkbar(1, 11, 10.4, 10.7),
                mkbar(2, 11.5, 10.5, 11.0), mkbar(3, 13, 9, 12.5)]
        events = detect_events("sh600001", bars, swings)
        core = [e for e in events if e["type"] in {"spring", "sign_of_strength"}]
        self.assertEqual(len(core), 2)
        self.assertTrue(all(e["status"] == PATH_AMBIGUOUS for e in core))

    def test_test_requires_prior_spring(self):
        # Reaction low with no spring before it is not a test.
        bars = [mkbar(0, 11, 10.5, 10.8), mkbar(1, 11, 10.4, 10.7),
                mkbar(2, 11.5, 10.5, 11.0), mkbar(3, 11, 10.3, 10.9)]
        swings = self.box_swings() + [swing(SWING_LOW, 10.3, index=3, confirm_index=5)]
        events = detect_events("sh600001", bars, swings)
        self.assertEqual([e for e in events if e["type"] == "test"], [])


class AnalyzeTests(unittest.TestCase):
    def test_descriptive_report_shape(self):
        bars = [mkbar(i, 10 + i / 2, 9 + i / 2, 9.5 + i / 2, volume=100 + i) for i in range(30)]
        report = analyze("sh600001", bars, as_of=bars[-1]["date"])
        self.assertEqual(report["analysis_type"], "descriptive_wyckoff_structure")
        self.assertEqual(report["rule_version"], DEFAULT_RULES["version"])
        self.assertEqual(report["status"], "descriptive_only")
        self.assertEqual(report["automated_signal"], "BLOCKED")
        self.assertEqual(report["candidate_selection"], "NOT_PERFORMED_BY_RULE_ENGINE")
        self.assertIsNone(report["target_price"]["value"])
        self.assertEqual(report["bar_count"], 30)
        for key in ("swings", "trend", "range", "events", "event_summary", "blockers"):
            self.assertIn(key, report)
        self.assertTrue(any("不是投资建议" in s for s in [report["statement"]]))

    def test_rejects_incomplete_bars(self):
        bars = [mkbar(0, 11, 9, 10), mkbar(1, 11, 9, 10, complete="false")]
        with self.assertRaisesRegex(ResearchError, "只接受已声明完整"):
            analyze("sh600001", bars)

    def test_short_series_reports_unknown_boundaries(self):
        bars = [mkbar(i, 12 - i, 9 - i, 10 - i) for i in range(3)]
        report = analyze("sh600001", bars)
        self.assertIsNone(report["range"])
        self.assertIn("insufficient_bars_for_structure", report["missing_dependencies"])
        self.assertIn("no_confirmed_range_boundary", report["missing_dependencies"])


if __name__ == "__main__":
    unittest.main()
