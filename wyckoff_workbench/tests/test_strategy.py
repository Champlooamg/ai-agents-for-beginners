import tempfile
import unittest
from unittest import mock

from wyckoff_cn.common import ResearchError
from wyckoff_cn.strategy import build_and_freeze, build_card, freeze_card
from wyckoff_cn.store import Store


def scenario_result(scenario="W-SPRING-TEST", state="spring_observed", support=10.0):
    return {"symbol": "sh600001", "scenario": scenario,
            "scenario_rule_version": "wyckoff-scenarios-1", "state": state,
            "matched_conditions": [{"condition": "spring", "event_id": "e1",
                                    "date": "2026-07-05", "status": "observed"}],
            "missing_conditions": ["test"],
            "invalidation": {"triggered": False, "reason": None,
                             "stop_level": support, "stop_basis": "range_support"},
            "note": None, "observation_window": "not_registered",
            "expiry_status": "unknown_window_not_registered",
            "auto_signal": "BLOCKED", "production_permission": False,
            "evidence_ids": ["v-e1"], "status": "candidate_unvalidated"}


def evaluation(scenario="W-SPRING-TEST", state="spring_observed", support=10.0, resistance=12.0):
    return {"symbol": "sh600001", "analysis_type": "descriptive_wyckoff_scenario_scan",
            "as_of": "2026-07-15", "structure_rule_version": "wyckoff-structure-1",
            "structure_rule_hash": "h", "scenario_rule_version": "wyckoff-scenarios-1",
            "support": support, "resistance": resistance, "trend_state": "consolidation",
            "scenarios": {scenario: scenario_result(scenario, state, support)},
            "status": "descriptive_only"}


class CardContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_card_is_conditional_and_keeps_unknowns(self):
        card = build_card(evaluation(), "W-SPRING-TEST", {"decision_at": "2026-10-09T08:00:00+08:00"})
        self.assertEqual(card["card_type"], "conditional_strategy_card")
        self.assertEqual(card["validity_status"], "candidate_unvalidated")
        self.assertEqual(card["entry"]["status"], "waiting")
        self.assertIsNone(card["target_price"]["value"])
        self.assertIsNone(card["profit_ratio"]["value"])
        self.assertIsNone(card["risk"]["position_size"])
        self.assertEqual(card["risk"]["position_size_status"], "withheld_until_all_dependencies_known")
        self.assertFalse(card["production_permission"])
        self.assertFalse(card["automatic_orders"])
        self.assertFalse(card["performance_shown"])
        self.assertEqual(card["holding_period"]["short_window"], "not_registered")
        self.assertIn("不是投资建议", card["statement"])

    def test_triggered_card_reports_confirmation_and_w_base_distance(self):
        card = build_card(evaluation("W-BASE", "breakup_observed"), "W-BASE",
                          {"decision_at": "2026-10-09T08:00:00+08:00"})
        self.assertEqual(card["validity_status"], "triggered_unvalidated")
        self.assertEqual(card["entry"]["status"], "confirmation_observed")
        self.assertEqual(card["entry"]["planned_entry"]["reference_level"], 12.0)
        self.assertEqual(card["risk"]["entry_to_stop_distance"], 2.0)
        self.assertEqual(card["risk"]["distance_status"], "computed_from_raw_structural_levels")
        self.assertEqual(card["invalidation"]["stop_level"], 10.0)

    def test_refuses_unformed_candidates_and_unknown_scenarios(self):
        for state in ("not_met", "structure_unknown"):
            with self.subTest(state=state), self.assertRaisesRegex(ResearchError, "未形成候选"):
                build_card(evaluation("W-BASE", state), "W-BASE", {})
        with self.assertRaisesRegex(ResearchError, "没有场景"):
            build_card(evaluation(), "W-LPS-BU", {})

    def test_freeze_rejects_production_target_and_position(self):
        card = build_card(evaluation(), "W-SPRING-TEST", {})
        with self.assertRaisesRegex(ResearchError, "禁止开启生产权限"):
            freeze_card(self.store, dict(card, production_permission=True))
        with self.assertRaisesRegex(ResearchError, "禁止给出目标价"):
            freeze_card(self.store, dict(card, target_price={"value": 15.0, "status": "x"}))
        with self.assertRaisesRegex(ResearchError, "禁止给出仓位数"):
            freeze_card(self.store, dict(card, risk=dict(card["risk"], position_size=100)))

    def test_freeze_appends_immutable_ledger_record(self):
        card = build_card(evaluation(), "W-SPRING-TEST", {})
        record = freeze_card(self.store, card)
        self.assertEqual(record["kind"], "strategy_card")
        self.assertEqual(record["payload"]["interpretation_status"], "conditional_unvalidated")
        self.assertTrue(record["payload"]["card_fingerprint"])
        self.assertTrue(record["payload"]["frozen_at"])
        self.assertEqual(len(self.store.records("strategy_card")), 1)

    def test_build_and_freeze_previews_without_writing_then_freezes(self):
        ev = evaluation()
        request = {"symbol": "sh600001", "scenario": "W-SPRING-TEST", "as_of": "2026-07-15",
                   "decision_at": "2026-10-09T08:00:00+08:00", "preview": True}
        with mock.patch("wyckoff_cn.scan.evaluate_symbol", return_value=ev):
            preview = build_and_freeze(self.store, request)
        self.assertTrue(preview["preview"])
        self.assertFalse(preview["frozen"])
        self.assertEqual(preview["evaluation"]["support"], 10.0)
        self.assertEqual(self.store.records("strategy_card"), [])
        with mock.patch("wyckoff_cn.scan.evaluate_symbol", return_value=ev):
            frozen = build_and_freeze(self.store, {k: v for k, v in request.items() if k != "preview"})
        self.assertTrue(frozen["frozen"])
        self.assertTrue(frozen["record_id"])
        self.assertEqual(frozen["card"]["symbol"], "sh600001")
        self.assertEqual(len(self.store.records("strategy_card")), 1)

    def test_audit_recomputes_frozen_strategy_cards(self):
        from wyckoff_cn.reports import audit
        card = build_card(evaluation(), "W-SPRING-TEST", {})
        freeze_card(self.store, card)
        result = audit(self.store)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["strategy_cards_checked"], 1)
        tampered = dict(card, production_permission=True)
        self.store.append("strategy_card", tampered)
        result = audit(self.store)
        self.assertFalse(result["ok"])
        self.assertEqual(result["strategy_cards_checked"], 2)
        self.assertTrue(any("生产权限非法" in e for e in result["errors"]))


if __name__ == "__main__":
    unittest.main()
