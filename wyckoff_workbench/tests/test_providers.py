import csv
import io
import json
import struct
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from wyckoff_cn.common import ResearchError
from wyckoff_cn.data import import_csv, quality
from wyckoff_cn.providers import (normalize_easy_tdx_daily_frame, parse_instock_eastmoney_response,
                                  parse_tdx_daily_package, parse_tdx_market_package,
                                  parse_tencent_daily_response,
                                  fetch_easy_tdx_daily, fetch_tdx_market, fetch_tencent_daily,
                                  rows_to_csv)
from wyckoff_cn.market import market_observation_csv, market_overview
from wyckoff_cn.research import calculate
from wyckoff_cn.reports import export_bundle
from wyckoff_cn.reports import audit
from wyckoff_cn.store import Store


def tdx_archive():
    codes = bytearray(150 * 150)
    blocks = bytearray(150 * 512)
    for i in range(150):
        rec = i * 150
        codes[rec:rec + 6] = ("600000" if i == 0 else f"700{i:03d}").encode()
        codes[rec + 32:rec + 34] = struct.pack("<H", i)
    codes[40:48] = "浦发银行".encode("gbk")
    block = memoryview(blocks)[:512]
    struct.pack_into("<d", block, 4, 10.0)
    struct.pack_into("<4d", block, 12, 10.1, 10.5, 10.0, 10.4)
    struct.pack_into("<Q", block, 56, 123400)
    struct.pack_into("<d", block, 72, 987654.0)
    output = io.BytesIO()
    empty_codes = bytearray(150 * 150)
    for i in range(150):
        rec = i * 150
        empty_codes[rec:rec + 6] = f"700{i:03d}".encode()
        empty_codes[rec + 32:rec + 34] = struct.pack("<H", i)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("sh261008.cod", codes)
        z.writestr("sh261008.md1", blocks)
        for market in ("sz", "bj"):
            z.writestr(market + "261008.cod", empty_codes)
            z.writestr(market + "261008.md1", bytearray(150 * 512))
    return output.getvalue()


def tdx_market_archive(ymd="2026-10-08", market_rows=None):
    output = io.BytesIO()
    date_suffix = ymd[2:4] + ymd[5:7] + ymd[8:10]
    # Include one priced ordinary share, one unpriced ordinary share and one
    # priced non-equity code per exchange, plus one reference-close-only
    # ordinary share in Shanghai. A trailing row mode of "reference_only"
    # writes just the close field (open/high/low/turnover stay zero) as seen in
    # real packages; "zero_ohlc_with_turnover" writes turnover without OHLC.
    if market_rows is None:
        market_rows = {
            "sh": [("600000", "浦发银行", 10.0, 10.4), ("688001", "华兴源创", 0.0, 0.0),
                   ("500001", "基金样本", 2.0, 2.1),
                   ("601059", "参考价样本", 15.56, 15.56, "reference_only")],
            "sz": [("000001", "平安银行", 12.0, 11.8), ("300001", "特锐德", 0.0, 0.0), ("127001", "转债样本", 100.0, 101.0)],
            "bj": [("920001", "新股样本", 20.0, 20.0), ("430001", "北交所无价", 0.0, 0.0), ("900001", "指数样本", 1.0, 1.1)],
        }
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for market, items in market_rows.items():
            codes = bytearray(150 * len(items))
            blocks = bytearray(512 * len(items))
            for index, item in enumerate(items):
                code, name, previous, close = item[:4]
                mode = item[4] if len(item) > 4 else ""
                rec = index * 150
                codes[rec:rec + 6] = code.encode("ascii")
                codes[rec + 32:rec + 34] = struct.pack("<H", index)
                encoded_name = name.encode("gbk")
                codes[rec + 40:rec + 40 + len(encoded_name)] = encoded_name
                block = memoryview(blocks)[index * 512:(index + 1) * 512]
                struct.pack_into("<d", block, 4, previous)
                if close > 0:
                    if mode == "reference_only":
                        struct.pack_into("<d", block, 36, close)
                    else:
                        open_ = previous if previous > 0 else close
                        high, low = max(open_, close), min(open_, close)
                        if mode == "zero_ohlc_with_turnover":
                            open_ = high = low = 0.0
                        struct.pack_into("<4d", block, 12, open_, high, low, close)
                        struct.pack_into("<Q", block, 56, 10000)
                        struct.pack_into("<d", block, 72, 100000.0)
            archive.writestr(f"{market}{date_suffix}.cod", codes)
            archive.writestr(f"{market}{date_suffix}.md1", blocks)
    return output.getvalue()


def tencent_response(bars=None, quote_ts="20261009161459", name="浦发银行",
                     symbol="sh600000", entry=None, msg="", code=0, data=None):
    if bars is None:
        bars = [["2026-10-08", "9.450", "9.700", "9.710", "9.410", "1150269.000"],
                ["2026-10-09", "9.700", "9.540", "9.740", "9.540", "947958.000"]]
    quote = [""] * 32
    quote[1] = name
    quote[30] = quote_ts
    if entry is None:
        entry = {"day": bars, "qt": {symbol: quote}}
    root = {"code": code, "msg": msg,
            "data": {symbol: entry} if data is None else data}
    return json.dumps(root, ensure_ascii=False).encode("utf-8")


class ProviderTests(unittest.TestCase):
    def test_easy_tdx_daily_frame_is_sorted_cut_off_and_current_bar_blocked(self):
        class Frame:
            columns = ["date", "open", "high", "low", "close", "vol", "amount"]

            def to_dict(self, orient):
                self.assert_orient = orient
                return [
                    {"date": "2026-10-09", "open": 10, "high": 11, "low": 9,
                     "close": 10.5, "vol": 1000, "amount": 10500},
                    {"date": "2026-10-08", "open": 9, "high": 10, "low": 8.5,
                     "close": 9.5, "vol": 900, "amount": 8550},
                    {"date": "2026-10-10", "open": 10, "high": 11, "low": 9,
                     "close": 10, "vol": 1000, "amount": 10000},
                ]

        rows = normalize_easy_tdx_daily_frame(
            Frame(), "sh600000", "2026-10-09", "2026-10-09T08:00:00+00:00")
        self.assertEqual([r["date"] for r in rows], ["2026-10-08", "2026-10-09"])
        self.assertEqual(rows[0]["complete"], "true")
        self.assertEqual(rows[1]["complete"], "false")
        self.assertEqual(rows[0]["volume_unit"], "shares")
        self.assertEqual(rows[0]["amount_unit"], "yuan")

    def test_easy_tdx_rejects_missing_columns_and_duplicate_dates(self):
        class Frame:
            columns = ["date", "open", "high", "low", "close", "vol", "amount"]
            def __init__(self, rows): self.rows = rows
            def to_dict(self, orient): return self.rows

        row = {"date": "2026-10-08", "open": 9, "high": 10, "low": 8.5,
               "close": 9.5, "vol": 900, "amount": 8550}
        captured = "2026-10-09T08:00:00+00:00"
        with self.assertRaisesRegex(ResearchError, "缺失"):
            normalize_easy_tdx_daily_frame(type("Bad", (), {"columns": [], "to_dict": lambda *_a, **_k: []})(),
                                           "sh600000", "2026-10-08", captured)
        with self.assertRaisesRegex(ResearchError, "重复日期"):
            normalize_easy_tdx_daily_frame(Frame([row, row]), "sh600000", "2026-10-08", captured)

    def test_easy_tdx_rejects_noncanonical_cutoff(self):
        class Frame:
            columns = ["date", "open", "high", "low", "close", "vol", "amount"]
            def to_dict(self, orient): return []

        with self.assertRaisesRegex(ResearchError, "YYYY-MM-DD"):
            normalize_easy_tdx_daily_frame(Frame(), "sh600000", "20261008", "2026-10-09T08:00:00+00:00")

    def test_vendored_easy_tdx_import_and_connector_without_distribution_metadata(self):
        import importlib.metadata
        import importlib
        import sys

        class Frame:
            columns = ["date", "open", "high", "low", "close", "vol", "amount"]
            def to_dict(self, orient):
                return [{"date": "2026-10-08", "open": 9, "high": 10, "low": 8.5,
                         "close": 9.5, "vol": 900, "amount": 8550}]

        class Client:
            _host, _port = "127.0.0.1", 7709
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def get_security_bars(self, *_args): return Frame()

        sys.modules.pop("easy_tdx", None)
        with patch("importlib.metadata.version", side_effect=importlib.metadata.PackageNotFoundError):
            # A vendored import must not ask package metadata for a separate distribution.
            easy_tdx = importlib.import_module("easy_tdx")
            self.assertEqual(easy_tdx.__version__, "1.20.8")
            with patch.object(easy_tdx, "TdxClient", Client), \
                 patch.object(easy_tdx, "Market", SimpleNamespace(SH=1, SZ=0, BJ=2)), \
                 patch.object(easy_tdx, "KlineCategory", SimpleNamespace(DAY=9)):
                result = fetch_easy_tdx_daily("sh600000", "2026-10-08", count=10)
        self.assertEqual(result["rows"][0]["date"], "2026-10-08")
        self.assertEqual(result["source_id"], "easy-tdx/1.20.8/security-bars-daily")

    def test_tdx_strict_parse_and_units(self):
        row = parse_tdx_daily_package(tdx_archive(), "2026-10-08", ["sh600000"])[0]
        self.assertEqual(row["volume"], 123400)
        self.assertEqual(row["volume_unit"], "shares")
        self.assertEqual(row["amount_unit"], "yuan")
        self.assertEqual(row["close"], 10.4)
        self.assertEqual(row["previous_close"], 10.0)

    def test_tdx_market_package_filters_a_shares_and_reports_unpriced_universe(self):
        parsed = parse_tdx_market_package(tdx_market_archive(), "2026-10-08",
                                          minimum_priced={"sh": 1, "sz": 1, "bj": 1})
        self.assertEqual([row["symbol"] for row in parsed["rows"]], ["bj920001", "sh600000", "sz000001"])
        self.assertEqual(parsed["unpriced_a_share_count"], 3)
        self.assertEqual(parsed["no_ohlc_a_share_count"], 1)
        self.assertEqual(parsed["a_share_code_count"], 7)
        self.assertTrue(parsed["sse_szse_bse_package_present"])
        self.assertEqual(parsed["market_counts"]["sz"]["priced_package_rows"], 2)
        self.assertEqual(parsed["rows"][1]["volume_unit"], "shares")
        self.assertEqual(parsed["rows"][1]["amount_unit"], "yuan")
        reference = parsed["no_ohlc_a_shares"][0]
        self.assertEqual(reference["symbol"], "sh601059")
        self.assertEqual(reference["reference_close"], 15.56)
        self.assertEqual(reference["reason"], "reference_close_only_no_ohlc_in_package")
        self.assertEqual(parsed["market_counts"]["sh"]["no_ohlc_a_shares"], 1)

    def test_tdx_daily_package_refuses_reference_close_only_row(self):
        with self.assertRaisesRegex(ResearchError, "参考收盘价|不能构造日线"):
            parse_tdx_daily_package(tdx_market_archive(), "2026-10-08", ["sh601059"])

    def test_tdx_market_package_still_rejects_zero_ohlc_with_turnover(self):
        rows = {
            "sh": [("600000", "浦发银行", 10.0, 10.4), ("600293", "异常样本", 3.93, 3.93, "zero_ohlc_with_turnover")],
            "sz": [("000001", "平安银行", 12.0, 11.8)],
            "bj": [("920001", "新股样本", 20.0, 20.0)],
        }
        with self.assertRaisesRegex(ResearchError, "OHLC/成交额不合法"):
            parse_tdx_market_package(tdx_market_archive(market_rows=rows), "2026-10-08",
                                     minimum_priced={"sh": 1, "sz": 1, "bj": 1})

    def test_tdx_market_package_rejects_missing_market_and_partial_floor(self):
        archive = tdx_market_archive()
        with self.assertRaisesRegex(ResearchError, "最低有价行阈值|低于来源完整包下限"):
            parse_tdx_market_package(archive, "2026-10-08", minimum_priced={"sh": 1, "sz": 1, "bj": 51})
        missing_bj = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(archive)) as source, zipfile.ZipFile(missing_bj, "w") as target:
            for name in source.namelist():
                if not name.startswith("bj"):
                    target.writestr(name, source.read(name))
        with self.assertRaisesRegex(ResearchError, "缺少BJ行情文件"):
            parse_tdx_market_package(missing_bj.getvalue(), "2026-10-08",
                                     minimum_priced={"sh": 1, "sz": 1, "bj": 1})

    def test_pre_bse_package_is_explicitly_partial(self):
        archive = tdx_market_archive("2022-05-05")
        partial = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(archive)) as source, zipfile.ZipFile(partial, "w") as target:
            for name in source.namelist():
                if not name.startswith("bj"):
                    target.writestr(name, source.read(name))
        parsed = parse_tdx_market_package(partial.getvalue(), "2022-05-05",
                                          minimum_priced={"sh": 1, "sz": 1, "bj": 1})
        self.assertEqual(parsed["market_scope"], ["sh", "sz"])
        self.assertFalse(parsed["sse_szse_bse_package_present"])
        self.assertEqual(parsed["market_counts"]["bj"]["floor_status"],
                         "not_applicable_before_bse_package_start")

    def test_fetch_tdx_market_preserves_explicit_source_and_payload_hash(self):
        import hashlib
        raw = tdx_market_archive()
        with patch("wyckoff_cn.providers.TDX_MIN_PRICED", {"sh": 1, "sz": 1, "bj": 1}), \
                patch("wyckoff_cn.providers._http_bytes", return_value=(raw, "https://example.invalid/package.zip", 200)) as request:
            result = fetch_tdx_market("2026-10-08")
        request.assert_called_once()
        self.assertEqual(result["raw"], raw)
        self.assertEqual(result["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(result["source_id"].endswith("/a_share_observation"))

    def test_market_overview_is_descriptive_and_does_not_emit_candidates(self):
        parsed = parse_tdx_market_package(tdx_market_archive(), "2026-10-08",
                                          minimum_priced={"sh": 1, "sz": 1, "bj": 1})
        coverage = {key: parsed[key] for key in ("market_counts", "market_scope",
            "sse_szse_bse_package_present", "priced_a_share_count", "unpriced_a_share_count",
            "no_ohlc_a_share_count", "a_share_code_count", "provider_completeness", "status_coverage")}
        coverage.update({"provider": "tdx_daily_package", "date": "2026-10-08"})
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            try:
                raw = tdx_market_archive()
                import hashlib
                provider_sha = hashlib.sha256(raw).hexdigest()
                source_id = "a-stock-data-3.10.1/tdx_daily_package/a_share_observation"
                store.retain_source_capture(source_id=source_id,
                    url="https://example.invalid/package.zip", raw=raw,
                    captured_at="2026-10-09T10:00:00+08:00", source_sha256=provider_sha,
                    row_count=len(parsed["rows"]), symbol_date="2026-10-08 all-market fixture")
                named = parsed["rows"] + parsed["unpriced_a_shares"] + parsed["no_ohlc_a_shares"]
                snapshot = import_csv(store, rows_to_csv(parsed["rows"]), {
                    "source_id": source_id,
                    "price_basis": "raw", "volume_unit": "shares", "amount_unit": "yuan",
                    "origin": {"provider_response_sha256": provider_sha,
                        "market_coverage": coverage,
                        "security_names": {row["symbol"]: row["name"] for row in named},
                        "unpriced_a_shares": parsed["unpriced_a_shares"],
                        "no_ohlc_a_shares": parsed["no_ohlc_a_shares"]}})
                result = market_overview(store, {"snapshot_id": snapshot["record_id"]})
                self.assertEqual(result["one_day_direction_counts"]["up"], 1)
                self.assertEqual(result["one_day_direction_counts"]["down"], 1)
                self.assertEqual(result["one_day_direction_counts"]["unchanged"], 1)
                self.assertEqual(result["universe"]["reference_close_only_a_shares"], 1)
                self.assertEqual(result["automated_candidate_selection"], "BLOCKED")
                self.assertIsNone(result["candidate_count"])
                self.assertEqual(result["strategy_cards_generated"], 0)
                self.assertTrue(audit(store)["ok"])
                self.assertEqual(audit(store)["market_snapshots_checked"], 1)
                observations = list(csv.DictReader(io.StringIO(
                    market_observation_csv(store, {"snapshot_id": snapshot["record_id"]}))))
                self.assertEqual(len(observations), 7)
                missing = next(row for row in observations if row["symbol"] == "bj430001")
                self.assertEqual(missing["observation_status"], "no_positive_close; trading_status_unknown")
                self.assertEqual(missing["close"], "")
                reference = next(row for row in observations if row["symbol"] == "sh601059")
                self.assertEqual(reference["observation_status"],
                                 "reference_close_only_no_ohlc; not_a_bar; trading_status_unknown")
                self.assertEqual(reference["close"], "15.56")
                self.assertEqual(reference["open"], "")
            finally:
                store.close()

    def test_tencent_daily_parses_unadjusted_rows_with_source_units(self):
        parsed = parse_tencent_daily_response(
            tencent_response(), "sh600000", "2026-10-09", "2026-10-09T10:00:00+00:00")
        self.assertEqual(parsed["name"], "浦发银行")
        self.assertEqual(parsed["price_basis"], "raw")
        rows = parsed["rows"]
        self.assertEqual([r["date"] for r in rows], ["2026-10-08", "2026-10-09"])
        self.assertEqual(rows[0]["close"], 9.70)
        self.assertEqual(rows[0]["volume"], 1150269.0)
        self.assertEqual(rows[0]["volume_unit"], "hands")
        self.assertIsNone(rows[0]["amount"])
        self.assertIsNone(rows[0]["previous_close"])
        self.assertEqual(rows[1]["previous_close"], 9.70)
        self.assertEqual(rows[0]["complete"], "true")
        self.assertEqual(rows[1]["complete"], "true")
        self.assertEqual(rows[1]["source_available_at"], "2026-10-09T16:14:59+08:00")
        self.assertEqual(rows[1]["availability_basis"], "provider_quote_timestamp")

    def test_tencent_daily_keeps_capture_day_bar_incomplete_before_session_close(self):
        parsed = parse_tencent_daily_response(
            tencent_response(quote_ts="20261009143000"), "sh600000", "2026-10-09",
            "2026-10-09T10:00:00+00:00")
        rows = parsed["rows"]
        self.assertEqual(rows[0]["complete"], "true")
        self.assertEqual(rows[1]["complete"], "false")
        self.assertIsNone(rows[1]["source_available_at"])
        self.assertEqual(rows[1]["availability_basis"], "unknown")

    def test_tencent_daily_refuses_adjusted_keys_empty_bars_and_contract_changes(self):
        captured = "2026-10-09T10:00:00+00:00"
        with self.assertRaisesRegex(ResearchError, "复权序列"):
            parse_tencent_daily_response(
                tencent_response(entry={"qfqday": [], "qt": {"sh600000": [""] * 32}}),
                "sh600000", "2026-10-09", captured)
        with self.assertRaisesRegex(ResearchError, "缺少day日线键"):
            parse_tencent_daily_response(
                tencent_response(entry={"qt": {"sh600000": [""] * 32}}),
                "sh600000", "2026-10-09", captured)
        with self.assertRaisesRegex(ResearchError, "返回空K线"):
            parse_tencent_daily_response(tencent_response(bars=[]), "sh600000", "2026-10-09", captured)
        with self.assertRaisesRegex(ResearchError, "错误信息"):
            parse_tencent_daily_response(tencent_response(msg="param error"), "sh600000", "2026-10-09", captured)
        with self.assertRaisesRegex(ResearchError, "未返回该证券K线数据"):
            parse_tencent_daily_response(tencent_response(data={}), "sh600000", "2026-10-09", captured)
        with self.assertRaisesRegex(ResearchError, "qt报价快照"):
            parse_tencent_daily_response(
                tencent_response(entry={"day": [["2026-10-08", "9.4", "9.7", "9.71", "9.41", "100"]]}),
                "sh600000", "2026-10-09", captured)

    def test_tencent_daily_rejects_bad_rows_and_leaks(self):
        captured = "2026-10-09T10:00:00+00:00"
        with self.assertRaisesRegex(ResearchError, "字段数不是6或7"):
            parse_tencent_daily_response(
                tencent_response(bars=[["2026-10-08", "9.4", "9.7", "9.71", "9.41"]]),
                "sh600000", "2026-10-09", captured)
        with self.assertRaisesRegex(ResearchError, "YYYY-MM-DD"):
            parse_tencent_daily_response(
                tencent_response(bars=[["20261008", "9.45", "9.7", "9.71", "9.41", "100"]]),
                "sh600000", "2026-10-09", captured)
        with self.assertRaisesRegex(ResearchError, "未严格递增"):
            parse_tencent_daily_response(
                tencent_response(bars=[["2026-10-08", "9.45", "9.7", "9.71", "9.41", "100"],
                                       ["2026-10-08", "9.45", "9.7", "9.71", "9.41", "100"]]),
                "sh600000", "2026-10-09", captured)
        with self.assertRaisesRegex(ResearchError, "观察截止日"):
            parse_tencent_daily_response(
                tencent_response(bars=[["2026-10-09", "9.45", "9.7", "9.71", "9.41", "100"]]),
                "sh600000", "2026-10-08", captured)
        with self.assertRaisesRegex(ResearchError, "OHLC不合法"):
            parse_tencent_daily_response(
                tencent_response(bars=[["2026-10-08", "9.4", "9.7", "9.41", "9.71", "100"]]),
                "sh600000", "2026-10-09", captured)
        with self.assertRaisesRegex(ResearchError, "不是有效UTF-8 JSON"):
            parse_tencent_daily_response(b"not-json", "sh600000", "2026-10-09", captured)

    def test_tencent_daily_preserves_corporate_action_annotation_and_checks_ex_date(self):
        captured = "2026-10-09T10:00:00+00:00"
        note = {"nd": "2025", "fh_sh": "4.2", "djr": "2026-07-15", "cqr": "2026-07-16",
                "FHcontent": "10派4.2元"}
        bars = [["2026-07-15", "9.45", "9.70", "9.71", "9.41", "100"],
                ["2026-07-16", "9.20", "9.25", "9.30", "9.15", "120", note]]
        parsed = parse_tencent_daily_response(
            tencent_response(bars=bars), "sh600000", "2026-10-09", captured)
        self.assertEqual(parsed["corporate_actions"],
                         [{"symbol": "sh600000", "date": "2026-07-16", "note": note}])
        self.assertEqual([r["date"] for r in parsed["rows"]], ["2026-07-15", "2026-07-16"])
        wrong_date = [list(bars[0]), ["2026-07-16", "9.2", "9.25", "9.3", "9.15", "120",
                                      dict(note, cqr="2026-07-15")]]
        with self.assertRaisesRegex(ResearchError, "除权除息日与所在K线日期不一致"):
            parse_tencent_daily_response(
                tencent_response(bars=wrong_date), "sh600000", "2026-10-09", captured)
        not_object = [list(bars[0]), ["2026-07-16", "9.2", "9.25", "9.3", "9.15", "120", 0.5]]
        with self.assertRaisesRegex(ResearchError, "第7字段不是公司行动注记对象"):
            parse_tencent_daily_response(
                tencent_response(bars=not_object), "sh600000", "2026-10-09", captured)

    def test_fetch_tencent_daily_preserves_raw_and_refuses_row_overflow(self):
        import hashlib
        raw = tencent_response()
        with patch("wyckoff_cn.providers._http_bytes",
                   return_value=(raw, "https://example.invalid/fqkline", 200)) as request:
            result = fetch_tencent_daily("sh600000", "2026-10-09", count=10)
        request.assert_called_once()
        self.assertIn("sh600000,day,,2026-10-09,10,", request.call_args.args[0])
        self.assertEqual(result["raw"], raw)
        self.assertEqual(result["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["source_id"], "tencent-fqkline/daily-unadjusted-readonly")
        self.assertEqual(result["rows_returned"], 2)
        self.assertEqual(result["volume_unit"], "hands")
        self.assertEqual(result["amount_unit"], "unknown")
        with patch("wyckoff_cn.providers._http_bytes", return_value=(raw, "https://example.invalid/fqkline", 200)):
            with self.assertRaisesRegex(ResearchError, "行数多于请求根数"):
                fetch_tencent_daily("sh600000", "2026-10-09", count=1)

    def test_tencent_rows_import_as_unadjusted_snapshot_without_amount(self):
        parsed = parse_tencent_daily_response(
            tencent_response(), "sh600000", "2026-10-09", "2026-10-09T10:00:00+00:00")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            try:
                snapshot = import_csv(store, rows_to_csv(parsed["rows"]), {
                    "source_id": "tencent-fqkline/daily-unadjusted-readonly",
                    "price_basis": "raw", "volume_unit": "hands", "amount_unit": "unknown",
                    "unit_basis": "腾讯fqkline不复权day字段；量为手，无历史成交额"})
                info = quality(snapshot)
                self.assertEqual(info["price_basis"], "raw")
                self.assertEqual(info["volume_unit"], "hands")
                self.assertEqual(info["missing_amount"], 2)
                self.assertEqual(info["rows_with_previous_close"], 1)
                self.assertEqual(info["unconfirmed_complete"], 0)
            finally:
                store.close()

    def test_tdx_previous_close_survives_csv_import_and_drives_single_bar_facts(self):
        row = parse_tdx_daily_package(tdx_archive(), "2026-10-08", ["sh600000"])[0]
        content = rows_to_csv([row])
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            try:
                snapshot = import_csv(store, content, {"source_id": "tdx-fixture",
                    "price_basis": "raw", "volume_unit": "shares", "amount_unit": "yuan"})
                imported = snapshot["payload"]["rows"][0]
                self.assertEqual(imported["previous_close"], 10.0)
                fact = calculate([imported], snapshot["record_id"],
                    {"version": "descriptive-facts-2", "volume_window": 20,
                     "relative_strength_window": 20})[0]
                self.assertAlmostEqual(fact["true_range"], 0.5)
                self.assertAlmostEqual(fact["gap_open"], 0.01)
                self.assertAlmostEqual(fact["net_progress"], 0.04)
                self.assertEqual(fact["previous_close_basis"], "row_previous_close")
                self.assertIn("previous_bar_ohlc", fact["missing_dependencies"])
            finally:
                store.close()

    def test_multi_day_easy_tdx_rows_are_imported_and_use_the_preceding_row(self):
        class Frame:
            columns = ["date", "open", "high", "low", "close", "vol", "amount"]
            def to_dict(self, orient):
                return [
                    {"date": "2026-10-07", "open": 9.0, "high": 10.0, "low": 8.5,
                     "close": 9.5, "vol": 900, "amount": 8550},
                    {"date": "2026-10-08", "open": 9.6, "high": 10.5, "low": 9.4,
                     "close": 10.4, "vol": 1200, "amount": 12480},
                ]

        rows = normalize_easy_tdx_daily_frame(
            Frame(), "sh600000", "2026-10-08", "2026-10-09T07:00:00+08:00")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            try:
                snapshot = import_csv(store, rows_to_csv(rows), {"source_id": "easy-tdx-fixture",
                    "price_basis": "raw", "volume_unit": "shares", "amount_unit": "yuan"})
                facts = calculate(snapshot["payload"]["rows"], snapshot["record_id"],
                    {"version": "descriptive-facts-2", "volume_window": 20,
                     "relative_strength_window": 20})
                self.assertEqual(len(facts), 2)
                self.assertAlmostEqual(facts[1]["net_progress"], 10.4 / 9.5 - 1)
                self.assertEqual(facts[1]["previous_close_basis"], "previous_input_row")
                self.assertEqual(facts[1]["previous_observation_date"], "2026-10-07")
            finally:
                store.close()

    def test_tdx_missing_symbol_does_not_fallback(self):
        with self.assertRaisesRegex(ResearchError, "未找到指定证券"):
            parse_tdx_daily_package(tdx_archive(), "2026-10-08", ["sz000001"])

    def test_instock_row_keeps_completeness_unknown(self):
        payload = {"data": {"klines": ["2026-10-08,10,10.4,10.5,9.9,100,1000"]}}
        row = parse_instock_eastmoney_response(json.dumps(payload).encode(), "sh600000", "2026-10-08")
        self.assertEqual(row["complete"], "unknown")
        self.assertEqual(row["volume"], 100)

    def test_local_capture_integrity_is_checked(self):
        raw = b"provider response"
        import hashlib
        sha = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            store.retain_source_capture(source_id="test", url="https://example.invalid",
                raw=raw, captured_at="2026-10-09T10:00:00+08:00", source_sha256=sha,
                row_count=1, symbol_date="sh600000@2026-10-08")
            self.assertTrue(store.verify()["ok"])
            with zipfile.ZipFile(io.BytesIO(export_bundle(store))) as bundle:
                self.assertIn("source_responses/" + sha + ".bin", bundle.namelist())
            (store.raw / (sha + ".bin")).write_bytes(b"changed")
            self.assertFalse(store.verify()["ok"])
            store.close()


if __name__ == "__main__":
    unittest.main()
