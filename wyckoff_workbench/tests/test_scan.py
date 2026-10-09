import unittest
from unittest import mock

from wyckoff_cn.common import ResearchError
from wyckoff_cn.scan import (INVALIDATED, SCENARIOS, STRUCTURE_UNKNOWN, _invalidation,
                             _match_sequence, evaluate)


def mk(i, high, low, close):
    return {"symbol": "sh600001", "date": "2026-07-%02d" % (i + 1), "open": (low + high) / 2,
            "high": high, "low": low, "close": close, "volume": 100, "amount": None,
            "previous_close": None, "complete": "true", "status": "unknown",
            "snapshot_id": "s%02d" % i}


def ev(id_, type_, index, status="observed", **extra):
    return {"id": id_, "type": type_, "bar_index": index, "date": "d%d" % index,
            "status": status, "evidence_id": "v-" + id_, "missing_dependencies": [], **extra}


def structure(events, trend="consolidation", support=10.0, resistance=12.0):
    return {"symbol": "sh600001", "as_of": "2026-07-15", "rule_version": "wyckoff-structure-1",
            "rule_hash": "h", "range": {"support": support, "resistance": resistance},
            "trend": {"state": trend}, "events": events, "status": "descriptive_only"}


class MatchTests(unittest.TestCase):
    def test_in_order_match_and_missing(self):
        events = [ev("e1", "spring", 5), ev("e2", "test", 8), ev("e3", "sign_of_strength", 12)]
        seq = SCENARIOS["W-SPRING-TEST"]["sequence"]
        matched = _match_sequence(events, seq)
        self.assertEqual([m["condition"] for m in matched], ["spring", "test"])
        self.assertEqual(matched[0]["event"]["id"], "e1")
        self.assertEqual(matched[1]["event"]["id"], "e2")

        matched = _match_sequence([ev("e1", "spring", 5)], seq)
        self.assertEqual(matched[1]["event"], None)

    def test_or_types_pick_earliest_and_skip_ambiguous(self):
        seq = SCENARIOS["W-BASE"]["sequence"]  # absorption = spring OR test
        events = [ev("amb", "spring", 5, status="path_ambiguous"),
                  ev("t", "test", 7), ev("s", "spring", 9)]
        matched = _match_sequence(events, seq)
        self.assertEqual(matched[0]["event"]["id"], "t")

    def test_conditions_respect_order(self):
        seq = SCENARIOS["W-SPRING-TEST"]["sequence"]
        events = [ev("test", "test", 5), ev("spring", "spring", 9)]
        matched = _match_sequence(events, seq)
        # The test cannot precede the spring, so the spring takes the later bar and
        # the test finds nothing after it.
        self.assertEqual(matched[0]["event"]["id"], "spring")
        self.assertEqual(matched[1]["event"], None)


class InvalidationTests(unittest.TestCase):
    def test_close_below_support_triggers(self):
        bars = [mk(i, 11, 10.5, c) for i, c in enumerate([11, 11, 11, 11, 11, 11, 9.5, 9.4])]
        matched = [{"condition": "absorption", "event": ev("e", "spring", 3)}]
        result = _invalidation("sh600001", bars, "W-BASE", structure([]), matched)
        self.assertTrue(result["triggered"])
        self.assertEqual(result["bar_index"], 6)
        self.assertEqual(result["stop_level"], 10.0)

    def test_no_break_below_support(self):
        bars = [mk(i, 11, 10.5, 10.8) for i in range(8)]
        matched = [{"condition": "absorption", "event": ev("e", "spring", 3)}]
        result = _invalidation("sh600001", bars, "W-BASE", structure([]), matched)
        self.assertFalse(result["triggered"])

    def test_lps_stop_is_broken_resistance(self):
        bars = [mk(i, 13, 11.5, c) for i, c in enumerate([12, 12, 12, 12, 12, 12, 11.4])]
        breakup = ev("b", "sign_of_strength", 3, broken_level=12.0)
        matched = [{"condition": "breakup", "event": breakup}]
        result = _invalidation("sh600001", bars, "W-LPS-BU", structure([]), matched)
        self.assertTrue(result["triggered"])
        self.assertEqual(result["stop_level"], 12.0)
        self.assertEqual(result["reason"], "close_below_broken_resistance")


class EvaluateGatingTests(unittest.TestCase):
    def test_no_confirmed_range_is_structure_unknown(self):
        bars = [mk(i, 10 + i / 2, 9 + i / 2, 9.5 + i / 2) for i in range(12)]
        result = evaluate("sh600001", bars)
        self.assertEqual(result["scenarios"]["W-BASE"]["state"], STRUCTURE_UNKNOWN)
        self.assertEqual(result["scenarios"]["W-SPRING-TEST"]["state"], STRUCTURE_UNKNOWN)
        # Every scenario result carries a validity status even on gating paths,
        # so market-wide filters never see a missing key.
        self.assertEqual(result["scenarios"]["W-BASE"]["status"], "not_met")

    def test_non_consolidation_trend_is_not_met(self):
        # A range exists (support/resistance confirmed) but the trend label is not a
        # consolidation, so the accumulation scenarios are reported not_met.
        spec = [(11, 10.5, 10.8), (11.5, 10.3, 11.0), (12, 10.4, 11.8), (11.5, 10.2, 10.6),
                (11, 10, 10.2), (11.2, 10.3, 10.9), (11, 9.4, 10.6), (11.3, 10.2, 11.0),
                (11, 10.1, 10.4), (11.2, 10.3, 10.9), (12.4, 11.5, 12.3), (12.8, 12.1, 12.6),
                (12.5, 12.1, 12.3)]
        bars = [mk(i, h, l, c) for i, (h, l, c) in enumerate(spec)]
        result = evaluate("sh600001", bars)
        self.assertEqual(result["scenarios"]["W-BASE"]["state"], "not_met")
        self.assertEqual(result["scenarios"]["W-SPRING-TEST"]["state"], "not_met")
        self.assertEqual(result["scenarios"]["W-BASE"]["status"], "not_met")


class EvaluateScenarioTests(unittest.TestCase):
    def bars(self):
        return [mk(i, 12.8, 10.5, 12.5) for i in range(15)]

    def patched(self, events):
        return mock.patch("wyckoff_cn.scan.analyze", return_value=structure(events))

    def test_spring_test_resolves_to_response(self):
        events = [ev("s", "spring", 5), ev("t", "test", 8),
                  ev("sos", "sign_of_strength", 12, broken_level=12.0)]
        with self.patched(events):
            result = evaluate("sh600001", self.bars())
        st = result["scenarios"]["W-SPRING-TEST"]
        self.assertEqual(st["state"], "response_observed")
        self.assertEqual([m["condition"] for m in st["matched_conditions"]],
                         ["spring", "test", "sign_of_strength"])
        self.assertEqual(st["missing_conditions"], [])
        self.assertEqual(st["status"], "candidate_unvalidated")

    def test_base_and_lps_states(self):
        events = [ev("s", "spring", 5), ev("t", "test", 8),
                  ev("sos", "sign_of_strength", 12, broken_level=12.0)]
        with self.patched(events):
            result = evaluate("sh600001", self.bars())
        base = result["scenarios"]["W-BASE"]
        self.assertEqual(base["state"], "breakup_observed")
        lps = result["scenarios"]["W-LPS-BU"]
        self.assertEqual(lps["state"], "breakup_observed")
        self.assertEqual(lps["missing_conditions"], ["lps"])

    def test_missing_test_reports_missing_and_state(self):
        events = [ev("s", "spring", 5)]
        with self.patched(events):
            result = evaluate("sh600001", self.bars())
        st = result["scenarios"]["W-SPRING-TEST"]
        self.assertEqual(st["state"], "spring_observed")
        self.assertEqual(st["missing_conditions"], ["test"])

    def test_invalidation_overrides_state(self):
        events = [ev("s", "spring", 3)]
        bars = [mk(i, 12, 10.5, 11.5) for i in range(6)] + [mk(6, 12, 9, 9.5)]
        with self.patched(events):
            result = evaluate("sh600001", bars)
        st = result["scenarios"]["W-SPRING-TEST"]
        self.assertEqual(st["state"], INVALIDATED)
        self.assertTrue(st["invalidation"]["triggered"])

    def test_report_is_descriptive_and_unranked(self):
        with self.patched([ev("s", "spring", 5)]):
            result = evaluate("sh600001", self.bars())
        self.assertEqual(result["status"], "descriptive_only")
        self.assertEqual(result["auto_signal"], "BLOCKED")
        self.assertEqual(result["candidate_selection"], "NOT_PERFORMED")
        self.assertIsNone(result["target_price"]["value"])
        self.assertIn("scenario_rule_version", result)


if __name__ == "__main__":
    unittest.main()
