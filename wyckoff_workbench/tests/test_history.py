import hashlib
import tempfile
import unittest

from wyckoff_cn.common import ResearchError, now
from wyckoff_cn.data import import_csv
from wyckoff_cn.history import (market_history, observed_bars, series_for, symbol_bars,
                                trading_calendar, universe_at)
from wyckoff_cn.providers import rows_to_csv
from wyckoff_cn.store import Store

MARKET_SOURCE = "test/tdx_daily_package/a_share_observation"


def bar(symbol, day, close, previous_close=None):
    return {"symbol": symbol, "date": day, "open": close - 0.05, "high": close + 0.1,
            "low": close - 0.1, "close": close, "volume": 1000, "amount": 10000.0,
            "previous_close": previous_close, "complete": "true", "status": "unknown"}


def market_day(store, day, priced=(), unpriced=(), no_ohlc=(), source_id=MARKET_SOURCE):
    names = {row["symbol"]: "名称" + row["symbol"][-4:] for row in priced}
    names.update({symbol: "名称" + symbol[-4:] for symbol in list(unpriced) + list(no_ohlc)})
    sh_counts = {"package_present": True, "priced_a_shares": len(priced),
                 "unpriced_a_shares": len(unpriced), "no_ohlc_a_shares": len(no_ohlc),
                 "a_share_codes": len(priced) + len(unpriced) + len(no_ohlc)}
    empty = {"package_present": True, "priced_a_shares": 0, "unpriced_a_shares": 0,
             "no_ohlc_a_shares": 0, "a_share_codes": 0}
    coverage = {"provider": "tdx_daily_package", "date": day,
        "market_counts": {"sh": sh_counts, "sz": dict(empty), "bj": dict(empty)},
        "market_scope": ["sh", "sz", "bj"], "sse_szse_bse_package_present": True,
        "priced_a_share_count": len(priced), "unpriced_a_share_count": len(unpriced),
        "no_ohlc_a_share_count": len(no_ohlc),
        "a_share_code_count": len(priced) + len(unpriced) + len(no_ohlc)}
    raw = ("package-" + day + "-" + source_id).encode()
    sha = hashlib.sha256(raw).hexdigest()
    captured_at = now()
    store.retain_source_capture(source_id=source_id,
        url="https://example.invalid/" + day + ".zip", raw=raw, captured_at=captured_at,
        source_sha256=sha, row_count=len(priced), symbol_date=day)
    origin = {"provider_response_sha256": sha, "market_coverage": coverage,
        "security_names": names,
        "unpriced_a_shares": [{"symbol": s, "name": names[s],
                               "reason": "no_positive_close_in_package", "status": "unknown"}
                              for s in unpriced],
        "no_ohlc_a_shares": [{"symbol": s, "name": names[s], "reference_close": 10.0,
                              "reason": "reference_close_only_no_ohlc_in_package", "status": "unknown"}
                             for s in no_ohlc]}
    return import_csv(store, rows_to_csv(list(priced)), {
        "source_id": source_id, "price_basis": "raw", "volume_unit": "shares",
        "amount_unit": "yuan", "origin": origin, "captured_at": captured_at})


def annotation_snapshot(store, symbol, day, note):
    rows = [bar(symbol, day, 10.0)]
    return import_csv(store, rows_to_csv(rows), {
        "source_id": "test/tencent-annotations", "price_basis": "raw",
        "volume_unit": "hands", "amount_unit": "unknown",
        "origin": {"corporate_actions": [{"symbol": symbol, "date": day, "note": note}]}})


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sunday, self.mon, self.tue, self.thu = ("2026-10-04", "2026-10-05",
                                                     "2026-10-06", "2026-10-08")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_trading_calendar_reports_only_observed_days_and_anomalies(self):
        market_day(self.store, self.sunday, priced=[bar("sh600001", self.sunday, 10.0)])
        market_day(self.store, self.mon, priced=[bar("sh600001", self.mon, 10.2)])
        market_day(self.store, self.tue, priced=[bar("sh600001", self.tue, 10.1)])
        market_day(self.store, self.thu, priced=[bar("sh600001", self.thu, 10.3)])
        calendar = trading_calendar(self.store)
        self.assertEqual(calendar["observed_trading_days"],
                         [self.sunday, self.mon, self.tue, self.thu])
        self.assertEqual(calendar["weekend_observations"], [self.sunday])
        self.assertEqual(calendar["unobserved_weekdays"], ["2026-10-07"])
        self.assertIn("不能解释为非交易日", calendar["statement"])
        self.assertEqual(calendar["non_trading_day_status"], "unknown_not_inferred_from_missing_snapshots")

    def test_duplicate_observed_dates_and_mixed_sources_refused(self):
        market_day(self.store, self.mon, priced=[bar("sh600001", self.mon, 10.2)])
        market_day(self.store, self.mon, priced=[bar("sh600002", self.mon, 20.2)])
        with self.assertRaisesRegex(ResearchError, "不静默选择或合并"):
            trading_calendar(self.store)
        self.store.close()
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        market_day(self.store, self.mon, priced=[bar("sh600001", self.mon, 10.2)])
        market_day(self.store, self.tue, priced=[bar("sh600001", self.tue, 10.3)],
                   source_id="test/other-source")
        with self.assertRaisesRegex(ResearchError, "禁止拼接不同来源"):
            trading_calendar(self.store)

    def test_pit_gate_refuses_snapshots_after_decision(self):
        market_day(self.store, self.mon, priced=[bar("sh600001", self.mon, 10.2)])
        with self.assertRaisesRegex(ResearchError, "不允许补签"):
            trading_calendar(self.store, {"decision_at": "2026-10-05T09:00:00+08:00"})

    def test_universe_at_reports_states_and_absent_symbols_with_unknown_cause(self):
        market_day(self.store, self.mon,
                   priced=[bar("sh600001", self.mon, 10.2)],
                   unpriced=["sh600002"], no_ohlc=["sh600003"])
        market_day(self.store, self.tue, priced=[bar("sh600001", self.tue, 10.1)])
        universe = universe_at(self.store, {"as_of": self.tue})
        self.assertEqual([m["symbol"] for m in universe["members"]], ["sh600001"])
        self.assertEqual(universe["members"][0]["observed_state"], "priced")
        absent = {item["symbol"]: item for item in universe["previously_observed_absent_on_date"]}
        self.assertEqual(set(absent), {"sh600002", "sh600003"})
        self.assertEqual(absent["sh600003"]["absence_status"], "cause_unknown_not_inferred")
        self.assertIn("st_status", universe["unknown_universe_attributes"])
        first = universe_at(self.store, {"as_of": self.mon})
        states = {m["symbol"]: m["state_detail"] for m in first["members"]}
        self.assertEqual(states["sh600002"], "no_positive_close_in_package")
        self.assertEqual(states["sh600003"], "reference_close_only_no_ohlc_in_package")
        with self.assertRaisesRegex(ResearchError, "不能据此判断该日是否为交易日"):
            universe_at(self.store, {"as_of": self.thu})

    def test_series_for_reports_missing_bars_discrepancies_and_annotations(self):
        market_day(self.store, self.mon, priced=[bar("sh600001", self.mon, 10.5, 10.0)])
        market_day(self.store, self.tue, unpriced=["sh600001"])
        market_day(self.store, self.thu, priced=[bar("sh600001", self.thu, 10.8, 10.2)])
        annotation_snapshot(self.store, "sh600001", self.thu, {"cqr": self.thu})
        result = series_for(self.store, {"symbol": "sh600001", "as_of": self.thu})
        self.assertEqual([b["date"] for b in result["bars"]], [self.mon, self.thu])
        self.assertEqual(result["bar_count"], 2)
        self.assertEqual(result["missing_bars"],
                         [{"date": self.tue, "observed_state": "unpriced"}])
        self.assertEqual(result["day_states"],
                         [{"date": self.mon, "observed_state": "priced"},
                          {"date": self.tue, "observed_state": "unpriced"},
                          {"date": self.thu, "observed_state": "priced"}])
        discrepancies = result["previous_close_discrepancies"]
        self.assertEqual(len(discrepancies), 1)
        self.assertEqual(discrepancies[0]["date"], self.thu)
        self.assertEqual(discrepancies[0]["prior_close"], 10.5)
        self.assertEqual(discrepancies[0]["previous_close"], 10.2)
        self.assertIn("not_adjusted", discrepancies[0]["interpretation"])
        self.assertEqual(discrepancies[0]["source_annotations"][0]["note"], {"cqr": self.thu})
        with self.assertRaisesRegex(ResearchError, "不能解释为该证券不存在"):
            series_for(self.store, {"symbol": "sh999999", "as_of": self.thu})

    def test_market_history_summarizes_membership_quality_and_gaps(self):
        market_day(self.store, self.mon,
                   priced=[bar("sh600001", self.mon, 10.5, 10.0), bar("sh600002", self.mon, 20.0)],
                   unpriced=["sh600003"])
        market_day(self.store, self.tue, unpriced=["sh600003"])
        market_day(self.store, self.thu,
                   priced=[bar("sh600001", self.thu, 10.8, 10.2)],
                   unpriced=["sh600003"])
        result = market_history(self.store, {"as_of": self.thu})
        self.assertEqual(result["window"]["observed_trading_days"], 3)
        self.assertEqual(result["symbol_count"], 3)
        self.assertEqual(len(result["days"]), 3)
        first = result["membership"]["sh600001"]
        self.assertEqual(first["priced_bars"], 2)
        self.assertEqual(first["bar_span"], [self.mon, self.thu])
        self.assertEqual(first["missing_bar_count"], 1)
        self.assertEqual(first["previous_close_discrepancy_count"], 1)
        second = result["membership"]["sh600002"]
        self.assertEqual(second["priced_bars"], 1)
        self.assertEqual(second["bar_span"], [self.mon, self.mon])
        self.assertEqual(second["last_observed"], self.mon)
        third = result["membership"]["sh600003"]
        self.assertEqual(third["priced_bars"], 0)
        self.assertEqual(third["unpriced_days"], 3)
        self.assertEqual(result["quality"]["missing_bars"], 1)
        self.assertEqual(result["quality"]["previous_close_discrepancies"], 1)
        self.assertEqual(result["quality"]["symbols_with_discrepancies"], 1)
        self.assertEqual(result["status"], "descriptive_only")
        self.assertIn("不是选股结果", result["statement"])

    def test_symbol_bars_folds_all_symbols_in_one_pass(self):
        market_day(self.store, self.mon,
                   priced=[bar("sh600001", self.mon, 10.5), bar("sh600002", self.mon, 20.0)],
                   unpriced=["sh600003"])
        market_day(self.store, self.tue, priced=[bar("sh600001", self.tue, 10.6)])
        bulk = symbol_bars(self.store, {"as_of": self.tue})
        self.assertEqual(bulk["window"], {"start": self.mon, "end": self.tue,
                                          "observed_trading_days": 2})
        self.assertEqual(sorted(bulk["series"]), ["sh600001", "sh600002", "sh600003"])
        self.assertEqual([b["date"] for b in bulk["series"]["sh600001"]["bars"]],
                         [self.mon, self.tue])
        self.assertEqual([b["date"] for b in bulk["series"]["sh600002"]["bars"]], [self.mon])
        self.assertEqual(bulk["series"]["sh600003"]["bars"], [])
        self.assertEqual(bulk["status"], "descriptive_only")

    def test_observed_bars_reports_basis_and_excludes_incomplete(self):
        market_day(self.store, self.mon, priced=[bar("sh600001", self.mon, 10.5)])
        market_day(self.store, self.tue, priced=[bar("sh600001", self.tue, 10.6)])
        history_basis = observed_bars(self.store, {"symbol": "sh600001", "as_of": self.tue})
        self.assertEqual(history_basis["basis"], "multi_day_market_history")
        self.assertEqual(history_basis["bar_count"], 2)
        self.assertEqual(history_basis["incomplete_bars_excluded"], 0)
        self.assertEqual(history_basis["price_basis"], "raw")
        rows = [dict(bar("sh600002", self.mon, 20.0), complete="false"),
                bar("sh600002", self.tue, 20.5)]
        snapshot = import_csv(self.store, rows_to_csv(rows), {
            "source_id": "test/partial-completeness", "price_basis": "raw",
            "volume_unit": "shares", "amount_unit": "yuan"})
        snapshot_basis = observed_bars(self.store, {"symbol": "sh600002",
                                                    "as_of": self.tue,
                                                    "snapshot_id": snapshot["record_id"]})
        self.assertEqual(snapshot_basis["basis"], "retained_snapshot_rows")
        self.assertEqual(snapshot_basis["snapshot_id"], snapshot["record_id"])
        self.assertEqual(snapshot_basis["bar_count"], 1)
        self.assertEqual(snapshot_basis["incomplete_bars_excluded"], 1)
        self.assertEqual([b["date"] for b in snapshot_basis["bars"]], [self.tue])
        with self.assertRaisesRegex(ResearchError, "没有已声明完整的日线"):
            observed_bars(self.store, {"symbol": "sh600002",
                                       "snapshot_id": snapshot["record_id"],
                                       "as_of": self.mon})
        with self.assertRaisesRegex(ResearchError, "没有观察到证券sh999999"):
            observed_bars(self.store, {"symbol": "sh999999", "snapshot_id": snapshot["record_id"]})


if __name__ == "__main__":
    unittest.main()
