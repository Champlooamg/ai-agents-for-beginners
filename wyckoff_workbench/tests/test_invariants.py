import csv
import io
import json
import math
import sqlite3
import tempfile
import unittest
import zipfile
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

from wyckoff_cn.bootstrap import bootstrap
from wyckoff_cn.common import ResearchError, digest, now
from wyckoff_cn.data import import_csv, quality
from wyckoff_cn.portfolio import order_guard
from wyckoff_cn.reports import audit, export_bundle
from wyckoff_cn.research import (DEFAULT_PROTOCOL, analysis, calculate, candidate_view,
                                 freeze_card, freeze_protocol, gate, register_candidate,
                                 transition_candidate)
from wyckoff_cn.store import Store


def rows(count=40):
    return [{"symbol": "sh600001", "date": (date(2026, 7, 1) + timedelta(days=i)).isoformat(),
             "open": 10 + i / 10, "high": 11 + i / 10,
             "low": 9 + i / 10, "close": 10.5 + i / 10,
             "volume": i + 1, "amount": None, "complete": "true", "status": "unknown",
             "source_available_at": None, "availability_basis": "unknown", "source_revision_id": "unknown"}
            for i in range(count)]


def csv_bytes(data):
    f = io.StringIO(newline="")
    w = csv.DictWriter(f, fieldnames=list(data[0]))
    w.writeheader()
    w.writerows(data)
    return f.getvalue().encode("utf-8")


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.data = rows()
        self.meta = {"source_id": "test-generated", "record_mode": "synthetic_demo",
                     "price_basis": "raw", "volume_unit": "shares", "amount_unit": "yuan"}
        self.snapshot = import_csv(self.store, csv_bytes(self.data), self.meta)
        self.protocol = freeze_protocol(self.store)
        self.request = {"snapshot_id": self.snapshot["record_id"], "protocol_id": self.protocol["record_id"],
                        "symbol": "sh600001", "as_of": self.data[-1]["date"], "record_mode": "synthetic_demo"}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def card(self, **updates):
        a = analysis(self.store, self.request)
        request = dict(self.request, market_context="合成测试；无市场观点", hypothesis="待验证假设",
                       alternative_hypothesis="竞争解释", invalidate_if="事前否定条件",
                       expected_response="待观察事实", risk_review="unknown", next_review="下一根",
                       supports=[a["facts"][-1]["evidence_id"]], opposes=[])
        request.update(updates)
        return freeze_card(self.store, request)

    def candidate(self):
        card = self.card()
        req = {"card_id": card["record_id"], "scenario": "W-SPRING-TEST", "structure_id": "s1",
               "event_family_id": "family1", "support": 9, "resistance": 16,
               "structure_start_date": "2026-07-01",
               "expiry_date": "2027-01-01", "test_rule": "人工确认测试", "response_rule": "人工确认响应",
               "invalidation_rule": "人工确认否定"}
        return register_candidate(self.store, req), req

    def observation_card(self, snapshot, as_of, support_latest=True):
        request = dict(self.request, snapshot_id=snapshot["record_id"], as_of=as_of)
        a = analysis(self.store, request)
        index = -1 if support_latest else -2
        request.update(market_context="合成测试；无市场观点", hypothesis="待验证假设",
                       alternative_hypothesis="竞争解释", invalidate_if="事前否定条件",
                       expected_response="待观察事实", risk_review="unknown", next_review="下一根",
                       supports=[a["facts"][index]["evidence_id"]], opposes=[])
        return freeze_card(self.store, request)

    def next_observation_snapshot(self):
        previous = self.observation_rows[-1] if hasattr(self, "observation_rows") else self.data[-1]
        day = date.fromisoformat(previous["date"]) + timedelta(days=1)
        next_row = deepcopy(previous)
        next_row.update(date=day.isoformat(), open=previous["close"] + 0.1,
                        high=previous["close"] + 1.0, low=previous["close"],
                        close=previous["close"] + 0.5, volume=previous["volume"] + 1)
        if not hasattr(self, "observation_rows"):
            self.observation_rows = deepcopy(self.data)
        self.observation_rows.append(next_row)
        snapshot = import_csv(self.store, csv_bytes(self.observation_rows),
                              dict(self.meta, source_id="synthetic-next-" + day.isoformat()))
        return snapshot, day.isoformat()

    def next_observation_card(self, support_latest=True):
        snapshot, day = self.next_observation_snapshot()
        return self.observation_card(snapshot, day, support_latest)

    def test_counterfactual_future_cannot_change_prefix(self):
        full = rows(80)
        first = calculate(full[:35], "snapshot-fixed", DEFAULT_PROTOCOL)
        mutated = deepcopy(full)
        for r in mutated[35:]:
            r.update(open=1000, high=4000, low=1, close=2500, volume=100000000)
        second = calculate(mutated, "snapshot-fixed", DEFAULT_PROTOCOL)[:35]
        self.assertEqual(digest(first), digest(second))

    def test_volume_reference_excludes_current_volume(self):
        r = rows(21)
        f1 = calculate(r, "same", DEFAULT_PROTOCOL)[-1]
        r[-1]["volume"] = 10000
        f2 = calculate(r, "same", DEFAULT_PROTOCOL)[-1]
        self.assertEqual(f1["volume_reference"], 10.5)
        self.assertEqual(f2["volume_reference"], 10.5)
        self.assertAlmostEqual(f2["relative_volume"], 10000 / 10.5)

    def test_warmup_missing_is_not_zero(self):
        f = calculate(rows(1), "same", DEFAULT_PROTOCOL)[0]
        self.assertIsNone(f["relative_volume"])
        self.assertIsNone(f["true_range"])
        self.assertIsNone(f["relative_strength_log"])

    def test_previous_close_fallback_is_formula_versioned_and_single_bar_only(self):
        single = rows(1)
        single[0]["previous_close"] = 10.0
        legacy = calculate(single, "same", {"version": "descriptive-facts-1",
            "volume_window": 20, "relative_strength_window": 20})[0]
        current = calculate(single, "same", {"version": "descriptive-facts-2",
            "volume_window": 20, "relative_strength_window": 20})[0]
        self.assertIsNone(legacy["net_progress"])
        self.assertAlmostEqual(current["net_progress"], single[0]["close"] / 10.0 - 1)
        self.assertEqual(current["previous_close_basis"], "row_previous_close")

        multi = rows(2)
        multi[1]["previous_close"] = 999.0
        latest = calculate(multi, "same", {"version": "descriptive-facts-2",
            "volume_window": 20, "relative_strength_window": 20})[1]
        self.assertAlmostEqual(latest["net_progress"], multi[1]["close"] / multi[0]["close"] - 1)
        self.assertEqual(latest["previous_close_basis"], "previous_input_row")

    def test_legacy_protocol_replays_frozen_facts_with_original_formula(self):
        legacy = freeze_protocol(self.store, dict(DEFAULT_PROTOCOL, version="descriptive-facts-1"))
        request = dict(self.request, protocol_id=legacy["record_id"])
        a = analysis(self.store, request)
        self.assertEqual(a["formula_version"], "descriptive-facts-1")
        self.assertNotIn("previous_close_basis", a["facts"][-1])
        request.update(market_context="合成测试", hypothesis="待验证", alternative_hypothesis="竞争解释",
            invalidate_if="否定条件", expected_response="待观察", risk_review="unknown", next_review="下一根",
            supports=[a["facts"][-1]["evidence_id"]], opposes=[])
        freeze_card(self.store, request)
        self.assertTrue(audit(self.store)["ok"])

    def test_new_custom_protocol_without_version_uses_current_formula(self):
        payload = dict(DEFAULT_PROTOCOL)
        payload.pop("version")
        protocol = freeze_protocol(self.store, payload)
        self.assertEqual(protocol["payload"]["version"], "descriptive-facts-2")

    def test_zero_range_keeps_location_unknown(self):
        r = rows(1)
        r[0].update(open=10, high=10, low=10, close=10)
        self.assertIsNone(calculate(r, "same", DEFAULT_PROTOCOL)[0]["close_location"])

    def test_relative_strength_requires_matching_dates(self):
        r = rows(25)
        bm = deepcopy(r)
        bm.pop(4)
        self.assertIsNone(calculate(r, "same", DEFAULT_PROTOCOL, bm)[24]["relative_strength_log"])

    def test_facts_stop_at_requested_date(self):
        a = analysis(self.store, dict(self.request, as_of=self.data[24]["date"]))
        self.assertEqual(len(a["facts"]), 25)
        self.assertEqual(a["facts"][-1]["date"], self.data[24]["date"])

    def test_future_cutoff_rejected(self):
        with self.assertRaises(ResearchError):
            analysis(self.store, dict(self.request, as_of="2099-01-01"))

    def test_noncanonical_cutoff_rejected_by_analysis_and_freeze(self):
        with self.assertRaisesRegex(ResearchError, "YYYY-MM-DD"):
            analysis(self.store, dict(self.request, as_of="20261008"))
        with self.assertRaisesRegex(ResearchError, "YYYY-MM-DD"):
            self.card(as_of="20261008")

    def test_noncanonical_candidate_expiry_rejected(self):
        card = self.card()
        req = {"card_id": card["record_id"], "scenario": "W-SPRING-TEST", "structure_id": "s2",
               "event_family_id": "family2", "support": 9, "resistance": 16,
               "structure_start_date": "2026-07-01",
               "expiry_date": "20270101", "test_rule": "测试", "response_rule": "响应",
               "invalidation_rule": "否定"}
        with self.assertRaisesRegex(ResearchError, "YYYY-MM-DD"):
            register_candidate(self.store, req)

    def test_audit_rejects_noncanonical_frozen_cutoff(self):
        card = self.card()
        self.store.connection.execute("DROP TRIGGER ledger_no_update")
        payload = self.store.get(card["record_id"], "card")["payload"]
        payload["as_of"] = "20260725"
        self.store.connection.execute("UPDATE ledger SET payload=? WHERE record_id=?",
                                      (json.dumps(payload), card["record_id"]))
        self.store.connection.commit()
        result = audit(self.store)
        self.assertFalse(result["ok"])
        self.assertTrue(any("YYYY-MM-DD" in e for e in result["errors"]))

    def test_backdated_decision_rejected(self):
        with self.assertRaises(ResearchError):
            analysis(self.store, dict(self.request, decision_at="2026-08-15T16:00:00+08:00"))

    def test_source_time_missing_blocks_historical_claim(self):
        s = import_csv(self.store, csv_bytes(self.data), dict(self.meta, record_mode="manual_observation_snapshot"))
        with self.assertRaisesRegex(ResearchError, "缺少来源首次可得时间"):
            analysis(self.store, dict(self.request, snapshot_id=s["record_id"], record_mode="historical_reconstruction"))

    def test_declared_source_times_do_not_certify_pit(self):
        r = deepcopy(self.data)
        for x in r:
            x["source_available_at"] = x["date"] + "T16:00:00+08:00"
            x["availability_basis"] = "test-declared-source-time"
        s = import_csv(self.store, csv_bytes(r), dict(self.meta, record_mode="manual_observation_snapshot"))
        with self.assertRaisesRegex(ResearchError, "未通过历史PIT验收"):
            analysis(self.store, dict(self.request, snapshot_id=s["record_id"], record_mode="historical_reconstruction"))

    def test_prospective_claim_rejected(self):
        with self.assertRaises(ResearchError):
            analysis(self.store, dict(self.request, record_mode="prospective_shadow"))

    def test_incomplete_earlier_bar_is_not_hidden(self):
        bad = deepcopy(self.data)
        bad[2]["complete"] = "false"
        s = import_csv(self.store, csv_bytes(bad), self.meta)
        with self.assertRaises(ResearchError):
            analysis(self.store, dict(self.request, snapshot_id=s["record_id"]))

    def test_amount_missing_preserved(self):
        q = quality(self.snapshot)
        self.assertEqual(q["missing_amount"], 40)
        self.assertEqual(q["zero_amount"], 0)

    def test_duplicates_rejected(self):
        with self.assertRaises(ResearchError):
            import_csv(self.store, csv_bytes(self.data + [self.data[-1]]), self.meta)

    def test_out_of_order_rejected(self):
        with self.assertRaises(ResearchError):
            import_csv(self.store, csv_bytes(list(reversed(self.data))), self.meta)

    def test_invalid_ohlc_rejected(self):
        bad = deepcopy(self.data)
        bad[3]["close"] = 1000
        with self.assertRaises(ResearchError):
            import_csv(self.store, csv_bytes(bad), self.meta)

    def test_previous_close_must_be_positive_when_supplied(self):
        bad = deepcopy(self.data)
        bad[0]["previous_close"] = 0
        with self.assertRaisesRegex(ResearchError, "previous_close必须为正数"):
            import_csv(self.store, csv_bytes(bad), self.meta)

    def test_nonfinite_number_rejected(self):
        bad = deepcopy(self.data)
        bad[3]["volume"] = "NaN"
        with self.assertRaises(ResearchError):
            import_csv(self.store, csv_bytes(bad), self.meta)

    def test_missing_time_basis_rejected(self):
        bad = deepcopy(self.data)
        bad[3]["source_available_at"] = "2026-07-04T16:00:00+08:00"
        with self.assertRaises(ResearchError):
            import_csv(self.store, csv_bytes(bad), self.meta)

    def test_no_timezone_rejected(self):
        with self.assertRaises(ResearchError):
            import_csv(self.store, csv_bytes(self.data), dict(self.meta, captured_at="2026-09-01T16:00:00"))

    def test_future_capture_rejected(self):
        with self.assertRaises(ResearchError):
            import_csv(self.store, csv_bytes(self.data), dict(self.meta, captured_at="2099-09-01T16:00:00+08:00"))

    def test_idempotent_import_preserves_first_ingestion(self):
        r = import_csv(self.store, csv_bytes(self.data), self.meta)
        self.assertEqual(r["record_id"], self.snapshot["record_id"])
        self.assertEqual(r["payload"]["ingested_at"], self.snapshot["payload"]["ingested_at"])

    def test_synthetic_not_mislabeled_as_real(self):
        with self.assertRaises(ResearchError):
            analysis(self.store, dict(self.request, record_mode="manual_observation_snapshot"))

    def test_freeze_cannot_accept_user_timestamp(self):
        with self.assertRaises(ResearchError):
            self.card(decision_at=now())

    def test_unknown_evidence_rejected(self):
        with self.assertRaises(ResearchError):
            self.card(supports=["fabricated-id"])

    def test_duplicate_evidence_rejected(self):
        e = analysis(self.store, self.request)["facts"][-1]["evidence_id"]
        with self.assertRaises(ResearchError):
            self.card(supports=[e, e])

    def test_append_revision_preserves_old_card(self):
        first = self.card()
        old_hash = first["record_hash"]
        second = self.card(supersedes_id=first["record_id"], revision_reason="新观察",
                           hypothesis="修订假设")
        self.assertEqual(self.store.get(first["record_id"])["record_hash"], old_hash)
        self.assertNotEqual(first["record_id"], second["record_id"])
        self.assertTrue(audit(self.store)["ok"])

    def test_database_refuses_update_and_delete(self):
        for query in ("UPDATE ledger SET kind='changed'", "DELETE FROM ledger"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.connection.execute(query)
            self.store.connection.rollback()

    def test_original_source_mutation_detected(self):
        p = self.store.raw / (self.snapshot["payload"]["source_sha256"] + ".bin")
        p.write_bytes(b"changed")
        self.assertFalse(audit(self.store)["ok"])

    def test_response_cannot_bypass_test(self):
        c, _ = self.candidate()
        card = self.card()
        with self.assertRaises(ResearchError):
            transition_candidate(self.store, {"candidate_id": c["record_id"], "card_id": card["record_id"],
                                              "new_state": "response_observed", "reason": "跳过测试"})

    def test_candidate_uses_new_decision_not_registration_card(self):
        c, req = self.candidate()
        with self.assertRaises(ResearchError):
            transition_candidate(self.store, {"candidate_id": c["record_id"], "card_id": req["card_id"],
                                              "new_state": "context_established", "reason": "复用旧卡"})

    def test_failed_candidate_retained_and_duplicate_refused(self):
        c, req = self.candidate()
        card = self.next_observation_card()
        transition_candidate(self.store, {"candidate_id": c["record_id"], "card_id": card["record_id"],
                                         "new_state": "invalidated", "reason": "已证伪"})
        self.assertEqual(candidate_view(self.store)[0]["state"], "invalidated")
        with self.assertRaises(ResearchError):
            register_candidate(self.store, req)
        self.assertEqual(len(self.store.records("candidate_attempt")), 2)
        self.assertEqual(self.store.records("candidate_attempt")[-1]["payload"]["status"], "rejected_duplicate")
        exported = zipfile.ZipFile(io.BytesIO(export_bundle(self.store)))
        events = json.loads(exported.read("ledger.json"))
        self.assertTrue(any(r["kind"] == "candidate_event" and r["payload"]["new_state"] == "invalidated" for r in events))

    def test_candidate_duplicate_cannot_be_bypassed_by_renaming_labels(self):
        c, req = self.candidate()
        renamed = dict(req, event_family_id="another-family", structure_id="renamed-structure")
        with self.assertRaisesRegex(ResearchError, "修改事件族或结构文本"):
            register_candidate(self.store, renamed)
        attempts = self.store.records("candidate_attempt")
        self.assertEqual([x["payload"]["status"] for x in attempts], ["accepted", "rejected_duplicate"])
        self.assertEqual(attempts[-1]["payload"]["related_candidate_ids"], [c["record_id"]])
        self.assertTrue(audit(self.store)["ok"])

    def test_expiry_cannot_be_claimed_early(self):
        c, _ = self.candidate()
        card = self.card()
        with self.assertRaises(ResearchError):
            transition_candidate(self.store, {"candidate_id": c["record_id"], "card_id": card["record_id"],
                                              "new_state": "expired", "reason": "提前到期"})

    def test_scene_must_pass_test_before_response(self):
        c, _ = self.candidate()
        for state in ("context_established", "test_observed", "response_observed"):
            card = self.next_observation_card()
            transition_candidate(self.store, {"candidate_id": c["record_id"], "card_id": card["record_id"],
                                              "new_state": state, "reason": "人工观察合成流程"})
        self.assertEqual(candidate_view(self.store)[0]["state"], "response_observed")
        self.assertTrue(audit(self.store)["ok"])

    def test_candidate_progression_requires_new_market_root_and_fresh_evidence(self):
        c, _ = self.candidate()
        first = self.next_observation_card()
        transition_candidate(self.store, {"candidate_id": c["record_id"], "card_id": first["record_id"],
                                          "new_state": "context_established", "reason": "观察新行情"})
        first_payload = self.store.get(first["record_id"], "card")["payload"]
        same_root = self.store.get(first_payload["snapshot_id"], "snapshot")
        same_root_card = self.observation_card(same_root, first["payload"]["last_bar_date"])
        with self.assertRaisesRegex(ResearchError, "采集或导入"):
            transition_candidate(self.store, {"candidate_id": c["record_id"],
                                              "card_id": same_root_card["record_id"],
                                              "new_state": "awaiting_test", "reason": "同根复用"})
        stale_support_card = self.next_observation_card(support_latest=False)
        with self.assertRaisesRegex(ResearchError, "最新行情证据"):
            transition_candidate(self.store, {"candidate_id": c["record_id"],
                                              "card_id": stale_support_card["record_id"],
                                              "new_state": "awaiting_test", "reason": "只引用旧根"})

    def test_candidate_progression_rejects_data_loaded_before_registration(self):
        initial_card = self.card()
        preloaded_snapshot, day = self.next_observation_snapshot()
        req = {"card_id": initial_card["record_id"], "scenario": "W-SPRING-TEST", "structure_id": "preloaded",
               "event_family_id": "family-preloaded", "support": 9, "resistance": 16,
               "structure_start_date": "2026-07-01", "expiry_date": "2027-01-01",
               "test_rule": "测试", "response_rule": "响应", "invalidation_rule": "否定"}
        c = register_candidate(self.store, req)
        observed_card = self.observation_card(preloaded_snapshot, day)
        with self.assertRaisesRegex(ResearchError, "候选登记/上次决策之后"):
            transition_candidate(self.store, {"candidate_id": c["record_id"],
                                              "card_id": observed_card["record_id"],
                                              "new_state": "context_established", "reason": "登记前已载入"})

    def test_audit_flags_legacy_candidate_progression_without_new_root(self):
        c, _ = self.candidate()
        card = self.store.get(c["payload"]["card_id"], "card")
        p = card["payload"]
        self.store.append("candidate_event", {
            "candidate_id": c["record_id"], "card_id": card["record_id"],
            "previous_state": "registered", "new_state": "context_established",
            "symbol": c["payload"]["symbol"], "event_family_id": c["payload"]["event_family_id"],
            "rule_hash": c["payload"]["rule_hash"], "event_date": p["last_bar_date"],
            "decision_at": p["decision_at"], "confirmed_at": p["decision_at"],
            "last_dependency_at": p["last_dependency_at"], "evidence_ids": p["manual"]["supports"]})
        result = audit(self.store)
        self.assertFalse(result["ok"])
        self.assertTrue(any("缺少新的行情快照根" in e for e in result["errors"]))

    def test_no_implicit_validation_upgrade(self):
        self.card()
        self.assertEqual(gate(self.store)["backtest_gate"], "BLOCK")
        self.assertFalse(gate(self.store)["production_permission"])

    def test_attached_sample_real_values_recomputed(self):
        r = bootstrap(self.store, Path(__file__).resolve().parents[1])
        a = analysis(self.store, {"snapshot_id": r["snapshot_id"], "protocol_id": r["protocol_id"],
                                 "symbol": "sh605366", "benchmark_symbol": "sh000300",
                                 "as_of": "2026-10-08"})
        f = a["facts"][-1]
        self.assertEqual(len(a["facts"]), 640)
        self.assertEqual(f["close"], 11.04)
        self.assertEqual(f["volume"], 953546)
        self.assertAlmostEqual(f["relative_volume"], 2.639605852638759)
        self.assertIsNone(f["amount"])
        self.assertEqual(a["backtest_status"], "BLOCK")

    def test_benchmark_dependencies_resolve_to_evidence(self):
        r = bootstrap(self.store, Path(__file__).resolve().parents[1])
        a = analysis(self.store, {"snapshot_id": r["snapshot_id"], "protocol_id": r["protocol_id"],
                                 "symbol": "sh605366", "benchmark_symbol": "sh000300", "as_of": "2026-10-08"})
        refs = {x["evidence_id"]: x for x in a["evidence"]}
        f = a["facts"][-1]
        self.assertTrue(set(f["dependency_ids"]).issubset(refs))
        self.assertEqual(sum(refs[x]["symbol"] == "sh000300" for x in f["dependency_ids"]), 2)


class OrderPlanTests(unittest.TestCase):
    def base(self):
        return {"side": "sell", "quantity": 100, "limit_price": 10, "buy_lot_size": 100,
                "decision_at": "2026-10-08T16:00:00+08:00",
                "earliest_order_at": "2026-10-09T09:30:00+08:00",
                "calendar_verified": True, "security_rules_verified": True, "risk_budget_verified": True,
                "reserved_quantity": 0, "inventory_lots": [{"bought_at": "2026-10-08T09:30:00+08:00",
                  "sellable_from": "2026-10-09T09:30:00+08:00", "remaining_quantity": 100}]}

    def test_t_plus_one_cannot_be_overridden_by_price(self):
        p = self.base()
        p["inventory_lots"][0]["sellable_from"] = "2026-10-08T15:00:00+08:00"
        g = order_guard(p)
        self.assertEqual(g["plan_constraints"], "BLOCK")
        self.assertTrue(any("当日" in r for r in g["reasons"]))

    def test_reserved_old_stock_cannot_be_sold_twice(self):
        p = self.base()
        p["reserved_quantity"] = 50
        self.assertEqual(order_guard(p)["plan_constraints"], "BLOCK")

    def test_valid_plan_is_never_a_fill(self):
        g = order_guard(self.base())
        self.assertEqual(g["plan_constraints"], "REVIEWABLE")
        self.assertEqual(g["fill_status"], "unknown")
        self.assertIsNone(g["realized_return"])

    def test_buy_needs_cash_fees_and_lot(self):
        p = dict(self.base(), side="buy", quantity=110, available_cash=1000, estimated_fees=5)
        self.assertEqual(order_guard(p)["plan_constraints"], "BLOCK")

    def test_same_instant_order_not_accepted(self):
        p = self.base()
        p["earliest_order_at"] = p["decision_at"]
        self.assertEqual(order_guard(p)["plan_constraints"], "BLOCK")

    def test_unknown_rule_calendar_block(self):
        p = self.base()
        p["calendar_verified"] = False
        self.assertEqual(order_guard(p)["plan_constraints"], "BLOCK")


if __name__ == "__main__":
    unittest.main()
