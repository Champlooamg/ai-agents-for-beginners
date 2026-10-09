"""Portable end-to-end checks: python tests/http_smoke.py (stdlib only)."""
import hashlib
import io
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


def run():
    project = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="wyckoff-http-test-") as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        server = subprocess.Popen([sys.executable, "-m", "wyckoff_cn", "--data-dir", directory,
                                   "serve", "--port", str(port)], cwd=project,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for _ in range(100):
                try:
                    html = urllib.request.urlopen(base, timeout=1).read().decode("utf-8")
                    break
                except urllib.error.URLError:
                    if server.poll() is not None:
                        raise RuntimeError(server.stderr.read().decode("utf-8", errors="replace"))
                    time.sleep(.1)
            else:
                raise RuntimeError("server startup timeout")
            token = re.search("const TOKEN='([^']+)'", html).group(1)
            checks = []

            def call(path, value=None, headers=None):
                h = {"Content-Type": "application/json", "X-Workbench-Token": token}
                h.update(headers or {})
                r = urllib.request.Request(base + path, data=json.dumps(value).encode("utf-8") if value is not None else None, headers=h)
                try:
                    with urllib.request.urlopen(r, timeout=10) as response:
                        return response.status, response.read()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read()

            def post(path, value):
                code, raw = call(path, value)
                if code != 200:
                    raise AssertionError((path, code, raw.decode("utf-8")))
                return json.loads(raw)

            state = json.loads(call("/api/state")[1])
            request = {"snapshot_id": state["snapshots"][0]["snapshot_id"],
                       "protocol_id": state["protocols"][0]["record_id"],
                       "symbol": "sh605366", "benchmark_symbol": "sh000300", "as_of": "2026-10-08"}
            a = post("/api/analyze", request)
            assert a["facts"][-1]["close"] == 11.04
            checks.append("archive_facts_http")
            assert call("/api/card", request, {"X-Workbench-Token": "invalid"})[0] == 403
            checks.append("csrf_rejection")
            assert call("/api/state", headers={"Host": "hostile.example"})[0] == 403
            checks.append("host_rebinding_rejection")
            assert call("/api/state", headers={"Origin": "https://hostile.example"})[0] == 403
            checks.append("cross_origin_rejection")
            fields = {k: "软件HTTP验收人工文本；未知保持unknown，不作投资判断。" for k in
                      ("market_context", "hypothesis", "alternative_hypothesis", "invalidate_if", "expected_response", "risk_review", "next_review")}
            card_request = dict(request, **fields, supports=[a["facts"][-1]["evidence_id"]], opposes=[])
            first = post("/api/card", card_request)
            checks.append("freeze_card_http")
            candidate = post("/api/candidate", {"card_id": first["record_id"], "scenario": "W-LPS-BU",
                "structure_id": "http-qa-structure", "event_family_id": "http-qa-family", "support": 8,
                "structure_start_date": "2026-10-01",
                "resistance": 12, "expiry_date": "2098-01-01", "test_rule": "软件验收文本",
                "response_rule": "软件验收文本", "invalidation_rule": "软件验收文本"})
            checks.append("candidate_http")
            card = post("/api/card", card_request)
            assert call("/api/transition", {"candidate_id": candidate["record_id"], "card_id": card["record_id"],
                                            "new_state": "response_observed", "reason": "绕过测试"})[0] == 400
            checks.append("state_bypass_rejection_http")
            card = post("/api/card", card_request)
            assert call("/api/transition", {"candidate_id": candidate["record_id"], "card_id": card["record_id"],
                                             "new_state": "context_established", "reason": "复用同一行情根"})[0] == 400
            checks.append("fresh_market_root_required_http")
            post("/api/risk", {"card_id": card["record_id"], "risk_type": "unknown-source", "reason": "流程验收"})
            structure = post("/api/structure", {"symbol": "sh605366", "as_of": "2026-10-08",
                                                "snapshot_id": request["snapshot_id"]})
            assert structure["rule_version"] == "wyckoff-structure-1"
            assert structure["bar_input"]["basis"] == "retained_snapshot_rows"
            assert structure["automated_signal"] == "BLOCKED"
            assert structure["candidate_selection"] == "NOT_PERFORMED_BY_RULE_ENGINE"
            checks.append("structure_http")
            scenarios = post("/api/scan-symbol", {"symbol": "sh605366", "as_of": "2026-10-08",
                                                  "snapshot_id": request["snapshot_id"]})
            assert scenarios["auto_signal"] == "BLOCKED"
            assert scenarios["target_price"]["value"] is None
            assert scenarios["scenarios"]["W-SPRING-TEST"]["status"] == "candidate_unvalidated"
            checks.append("scenario_scan_http")
            card_request_wy = {"symbol": "sh605366", "scenario": "W-SPRING-TEST", "as_of": "2026-10-08",
                               "snapshot_id": request["snapshot_id"]}
            preview = post("/api/strategy-card", dict(card_request_wy, preview=True))
            assert preview["preview"] and not preview["frozen"]
            assert preview["card"]["target_price"]["value"] is None
            assert preview["card"]["risk"]["position_size"] is None
            checks.append("strategy_card_preview_http")
            frozen = post("/api/strategy-card", card_request_wy)
            assert frozen["frozen"] and frozen["card"]["validity_status"] == "candidate_unvalidated"
            code, raw = call("/api/strategy-card?id=" + frozen["record_id"])
            assert code == 200 and json.loads(raw)["card_fingerprint"]
            state2 = json.loads(call("/api/state")[1])
            assert any(x["record_id"] == frozen["record_id"] for x in state2["strategy_cards"])
            checks.append("strategy_card_freeze_http")
            for path, value in (("/api/market-history", {}), ("/api/universe", {"as_of": "2026-10-08"}),
                                ("/api/scan", {"as_of": "2026-10-08"}),
                                ("/api/structure", {"symbol": "sh605366", "as_of": "2026-10-08"})):
                code, raw = call(path, value)
                assert code == 400, (path, code, raw.decode("utf-8"))
            checks.append("market_views_require_market_snapshots_http")
            assert json.loads(call("/api/calendar", {})[1])["observed_count"] == 0
            checks.append("calendar_http")
            assert json.loads(call("/api/audit")[1])["ok"]
            checks.append("audit_http")
            code, raw = call("/api/export")
            assert code == 200
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                manifest = json.loads(z.read("export_manifest.json"))
                for name, sha in manifest["sha256"].items():
                    assert hashlib.sha256(z.read(name)).hexdigest() == sha
            checks.append("export_hashes_http")
            assert "BLOCK" in call("/api/report")[1].decode("utf-8")
            checks.append("blocked_performance_report_http")
            return {"passed": True, "count": len(checks), "checks": checks,
                    "scope": "HTTP API end-to-end; not browser visual testing"}
        finally:
            server.terminate()
            server.wait(timeout=10)
            if server.stdout:
                server.stdout.close()
            if server.stderr:
                server.stderr.close()


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
