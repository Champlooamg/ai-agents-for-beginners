import csv
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date, timedelta

from wyckoff_cn.common import ResearchError, now
from wyckoff_cn.data import import_csv
from wyckoff_cn.external import add_external_note, external_note_text
from wyckoff_cn.reports import export_bundle
from wyckoff_cn.research import analysis, freeze_protocol
from wyckoff_cn.store import Store


def sample_csv():
    rows = []
    for i in range(45):
        close = 10 + i / 10
        rows.append({"symbol": "sh600000", "date": (date(2026, 8, 1) + timedelta(days=i)).isoformat(),
            "open": close, "high": close + 0.5, "low": close - 0.5, "close": close,
            "volume": 1000 + i, "amount": "", "complete": "true", "status": "unknown",
            "source_available_at": "", "availability_basis": "unknown", "source_revision_id": "unknown"})
    f = io.StringIO(newline="")
    writer = csv.DictWriter(f, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return f.getvalue().encode()


class ExternalNoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.snapshot = import_csv(self.store, sample_csv(), {"source_id": "test", "price_basis": "raw",
            "volume_unit": "shares", "record_mode": "synthetic_demo"})
        self.protocol = freeze_protocol(self.store)
        self.request = {"snapshot_id": self.snapshot["record_id"], "protocol_id": self.protocol["record_id"],
            "symbol": "sh600000", "as_of": "2026-09-14", "record_mode": "synthetic_demo",
            "decision_at": now()}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def note(self, **updates):
        payload = {"source_tool": "tradingagents_astock", "title": "外部分析", "content": "BUY!!! 未验证文本",
            "source_reported_date": "unknown", "source_ref": "job-123"}
        payload.update(updates)
        return add_external_note(self.store, payload)

    def test_external_output_is_archived_but_does_not_change_analysis(self):
        before = analysis(self.store, self.request)
        record = self.note()
        after = analysis(self.store, self.request)
        self.assertEqual(before, after)
        self.assertEqual(external_note_text(self.store, record["record_id"]), "BUY!!! 未验证文本")
        self.assertEqual(self.store.records("candidate"), [])
        self.assertEqual(self.store.records("snapshot"), [self.snapshot])
        self.assertIn("not_market_evidence", record["payload"]["qualification"])

    def test_future_report_date_is_refused(self):
        with self.assertRaisesRegex(ResearchError, "不能在未来"):
            self.note(source_reported_date="2099-01-01")

    def test_invalid_unicode_and_oversize_are_refused(self):
        with self.assertRaisesRegex(ResearchError, "无效Unicode"):
            self.note(content="bad\ud800text")
        with self.assertRaisesRegex(ResearchError, "上限1MiB"):
            self.note(content="x" * (1024 * 1024 + 1))

    def test_tampering_is_detected_and_raw_is_exported(self):
        record = self.note()
        sha = record["payload"]["content_sha256"]
        path = self.store.raw / ("external-" + sha + ".txt")
        with zipfile.ZipFile(io.BytesIO(export_bundle(self.store))) as bundle:
            self.assertEqual(bundle.read("external_notes/" + sha + ".txt"), "BUY!!! 未验证文本".encode())
        path.write_text("changed", encoding="utf-8")
        self.assertFalse(self.store.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
