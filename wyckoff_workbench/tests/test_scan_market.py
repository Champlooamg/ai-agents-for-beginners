import tempfile
import unittest
from unittest import mock

from test_history import bar, market_day

from wyckoff_cn.common import ResearchError
from wyckoff_cn.scan import evaluate_symbol, scan_market, structure_symbol
from wyckoff_cn.store import Store

DAYS = ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-05", "2026-10-06", "2026-10-08"]


def fake_evaluation(symbol, bars, as_of=None):
    def scenario(state, status):
        return {"symbol": symbol, "scenario": "x", "scenario_rule_version": "wyckoff-scenarios-1",
                "state": state, "matched_conditions": [], "missing_conditions": [],
                "invalidation": {"triggered": False, "stop_level": 10.0},
                "status": status}
    return {"symbol": symbol, "as_of": as_of, "structure_rule_version": "wyckoff-structure-1",
            "structure_rule_hash": "h", "scenario_rule_version": "wyckoff-scenarios-1",
            "support": 10.0, "resistance": 12.0, "trend_state": "consolidation",
            "scenarios": {"W-BASE": scenario("absorption_observed", "candidate_unvalidated"),
                          "W-SPRING-TEST": scenario("base_forming", "not_met"),
                          "W-LPS-BU": scenario("awaiting_breakup", "not_met")},
            "status": "descriptive_only"}


class ScanMarketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        for day in DAYS:
            priced = [bar("sh600001", day, 10.0 + DAYS.index(day) / 10)]
            unpriced = []
            if day == DAYS[-1]:
                priced.append(bar("sh600002", day, 20.0))
                unpriced.append("sh600003")
            market_day(self.store, day, priced=priced, unpriced=unpriced)
        self.request = {"as_of": DAYS[-1]}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_scan_market_counts_unqualified_and_reports_omissions(self):
        with mock.patch("wyckoff_cn.scan.evaluate", side_effect=fake_evaluation):
            result = scan_market(self.store, self.request)
        self.assertEqual(result["member_count"], 3)
        self.assertEqual(result["scanned_count"], 1)
        self.assertEqual(result["unqualified_count"], 2)
        self.assertEqual(result["unqualified_reason_counts"], {"insufficient_complete_bars": 2})
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["candidates_omitted_not_matched"], 0)
        self.assertFalse(result["ranked_by_score"])
        self.assertEqual(result["auto_signal"], "BLOCKED")
        self.assertEqual([c["symbol"] for c in result["candidates"]], ["sh600001"])
        self.assertEqual({u["symbol"] for u in result["unqualified"]}, {"sh600002", "sh600003"})
        with mock.patch("wyckoff_cn.scan.evaluate", side_effect=fake_evaluation):
            trimmed = scan_market(self.store, dict(self.request, include="candidates_only"))
        self.assertEqual(trimmed["unqualified"], [])
        self.assertEqual(trimmed["unqualified_list_omitted"], 2)
        self.assertEqual(trimmed["candidates_omitted_not_matched"], 0)

    def test_scan_market_refuses_unknown_include(self):
        with self.assertRaisesRegex(ResearchError, "include只能是all或candidates_only"):
            scan_market(self.store, dict(self.request, include="ranked"))

    def test_evaluate_symbol_and_structure_symbol_report_bar_input(self):
        structure = structure_symbol(self.store, {"symbol": "sh600001", "as_of": DAYS[-1]})
        self.assertEqual(structure["analysis_type"], "descriptive_wyckoff_structure")
        self.assertEqual(structure["rule_version"], "wyckoff-structure-1")
        self.assertEqual(structure["bar_input"]["basis"], "multi_day_market_history")
        self.assertEqual(structure["bar_input"]["bar_count"], 6)
        self.assertEqual(structure["bar_input"]["incomplete_bars_excluded"], 0)
        self.assertEqual(structure["automated_signal"], "BLOCKED")
        evaluation = evaluate_symbol(self.store, {"symbol": "sh600001", "as_of": DAYS[-1]})
        self.assertEqual(evaluation["analysis_type"], "descriptive_wyckoff_scenario_scan")
        self.assertEqual(evaluation["bar_input"]["bar_count"], 6)
        self.assertEqual(set(evaluation["scenarios"]), {"W-BASE", "W-SPRING-TEST", "W-LPS-BU"})
        with self.assertRaisesRegex(ResearchError, "数据不足"):
            evaluate_symbol(self.store, {"symbol": "sh600002", "as_of": DAYS[-1]})


if __name__ == "__main__":
    unittest.main()
