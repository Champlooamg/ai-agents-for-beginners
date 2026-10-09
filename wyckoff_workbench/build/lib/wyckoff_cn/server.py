import json
import secrets
import socket
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .common import ResearchError, now
from .data import import_csv, quality
from .external import add_external_note, external_note_summary, external_note_text
from .market import market_observation_csv, market_overview
from .providers import (fetch_easy_tdx_daily, fetch_instock_daily, fetch_tdx_daily,
                        fetch_tdx_market, rows_to_csv)
from .portfolio import save_order_plan
from .reports import audit, card_markdown, daily_report, export_bundle
from .research import (analysis, candidate_view, freeze_card, freeze_protocol, gate,
                       record_risk, register_candidate, transition_candidate)
from .store import Store


def _is_json_content_type(headers):
    return headers.get_content_type().lower() == "application/json"


def serve(directory, project, port=8765, open_browser=False):
    csrf = secrets.token_urlsafe(32)
    hosts = {"127.0.0.1:" + str(port), "localhost:" + str(port)}
    origins = {"http://" + h for h in hosts}
    html = (Path(__file__).parent / "workbench.html").read_text(encoding="utf-8").replace("__CSRF_TOKEN__", csrf)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Avoid logging uploaded data or request query values.
            pass

        def send(self, status, body, content_type="application/json; charset=utf-8", extra_headers=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; connect-src 'self'; img-src 'self' data:")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, status, value):
            self.send(status, json.dumps(value, ensure_ascii=False, allow_nan=False))

        def host_allowed(self):
            if self.headers.get("Host") not in hosts:
                self.send_json(403, {"error": "本地工作台仅允许localhost/127.0.0.1"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in origins:
                self.send_json(403, {"error": "拒绝跨站访问"})
                return False
            return True

        def do_GET(self):
            if not self.host_allowed():
                return
            u = urlparse(self.path)
            if u.path == "/":
                self.send(200, html, "text/html; charset=utf-8")
                return
            store = Store(directory)
            try:
                if u.path == "/api/state":
                    snapshots = store.records("snapshot")
                    market_snapshots = []
                    for record in snapshots:
                        origin = record["payload"].get("origin")
                        coverage = origin.get("market_coverage") if isinstance(origin, dict) else None
                        if isinstance(coverage, dict):
                            market_snapshots.append({"snapshot_id": record["record_id"],
                                "source_id": record["payload"]["source_id"],
                                "as_of": coverage.get("date"),
                                "priced_a_share_count": coverage.get("priced_a_share_count"),
                                "market_scope": coverage.get("market_scope", []),
                                "all_exchanges_present": coverage.get("sse_szse_bse_package_present", False)})
                    self.send_json(200, {"version": "0.6.3.0", "now": now(),
                        "snapshots": [quality(r) for r in snapshots],
                        "market_snapshots": market_snapshots,
                        "source_captures": [r["payload"] for r in store.records("source_capture")],
                        "external_notes": [external_note_summary(r) | {"record_id": r["record_id"]}
                                           for r in store.records("external_note")],
                        "protocols": [{"record_id": r["record_id"], "payload": r["payload"]} for r in store.records("protocol")],
                        "cards": [{"record_id": r["record_id"], "symbol": r["payload"]["symbol"],
                                   "as_of": r["payload"]["as_of"], "decision_at": r["payload"]["decision_at"],
                                   "hypothesis": r["payload"]["manual"]["hypothesis"]} for r in store.records("card")],
                        "candidates": candidate_view(store),
                        "candidate_attempts": [{"record_id": r["record_id"],
                                                "symbol": r["payload"]["symbol"],
                                                "scenario": r["payload"]["scenario"],
                                                "status": r["payload"]["status"],
                                                "related_candidate_ids": r["payload"]["related_candidate_ids"]}
                                               for r in store.records("candidate_attempt")],
                        "gate": gate(store),
                        "risk_count": len(store.records("risk")), "order_plan_count": len(store.records("order_plan"))})
                elif u.path == "/api/audit":
                    self.send_json(200, audit(store))
                elif u.path == "/api/card":
                    ref = parse_qs(u.query).get("id", [""])[0]
                    self.send(200, card_markdown(store.get(ref, "card")), "text/markdown; charset=utf-8")
                elif u.path == "/api/external-note":
                    ref = parse_qs(u.query).get("id", [""])[0]
                    body = external_note_text(store, ref)
                    self.send(200, body, "text/plain; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{ref}.txt"'})
                elif u.path == "/api/market-observations":
                    ref = parse_qs(u.query).get("id", [""])[0]
                    body = market_observation_csv(store, {"snapshot_id": ref})
                    self.send(200, body, "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="market-observations.csv"'})
                elif u.path == "/api/report":
                    self.send(200, daily_report(store), "text/markdown; charset=utf-8")
                elif u.path == "/api/export":
                    self.send(200, export_bundle(store), "application/zip")
                else:
                    self.send_json(404, {"error": "路径不存在"})
            except ResearchError as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception:
                self.send_json(500, {"error": "操作失败；请用命令行doctor检查本地数据"})
            finally:
                store.close()

        def do_POST(self):
            if not self.host_allowed():
                return
            if self.headers.get("X-Workbench-Token") != csrf:
                self.send_json(403, {"error": "本地会话令牌无效；请刷新页面"})
                return
            if not _is_json_content_type(self.headers):
                self.send_json(415, {"error": "请求Content-Type必须为application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 30 * 1024 * 1024:
                    raise ResearchError("请求大小必须为1字节至30MiB")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ResearchError("请求必须是JSON对象")
            except (ValueError, ResearchError, UnicodeDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            store = Store(directory)
            try:
                path = urlparse(self.path).path
                if path in ("/api/fetch/tdx", "/api/fetch/tdx-market", "/api/fetch/instock", "/api/fetch/easy-tdx"):
                    if path == "/api/fetch/tdx-market":
                        day = request.get("date")
                        if not isinstance(day, str):
                            raise ResearchError("全市场TDX接口需要一个收盘日期")
                        fetched = fetch_tdx_market(day)
                        rows, source_id = fetched["rows"], fetched["source_id"]
                        volume_unit, amount_unit = "shares", "yuan"
                        unit_basis = "a-stock-data 3.10.1 §1.3 TDX包字段口径；有价行单位声明，未独立认证"
                        coverage = {key: fetched[key] for key in (
                            "market_counts", "market_scope", "sse_szse_bse_package_present",
                            "priced_a_share_count", "unpriced_a_share_count", "a_share_code_count",
                            "provider_completeness", "status_coverage")}
                        coverage.update({"provider": "tdx_daily_package", "date": day,
                                         "market_universe_status": "provider_list_not_exchange_certified"})
                        names = {row["symbol"]: row["name"] for row in rows}
                        unpriced = fetched["unpriced_a_shares"]
                        names.update({row["symbol"]: row["name"] for row in unpriced})
                        origin = {"provider_response_sha256": fetched["source_sha256"],
                                  "source_completeness": fetched["provider_completeness"],
                                  "market_coverage": coverage, "security_names": names,
                                  "unpriced_a_shares": unpriced}
                        symbol_date = f"{day}: all packaged ordinary A shares; priced={len(rows)}; unpriced={len(unpriced)}"
                    elif path == "/api/fetch/tdx":
                        day = request.get("date")
                        symbols = request.get("symbols")
                        if not isinstance(day, str) or not isinstance(symbols, list) or not 1 <= len(symbols) <= 5:
                            raise ResearchError("TDX接口需要日期及1至5个证券代码")
                        fetched = fetch_tdx_daily(day, symbols)
                        rows, source_id = fetched["rows"], fetched["source_id"]
                        volume_unit, amount_unit = "shares", "yuan"
                        unit_basis = "a-stock-data 3.10.1 §1.3 TDX日线包字段口径；仅作来源声明，未独立认证"
                        origin = {"provider_response_sha256": fetched["source_sha256"],
                                  "source_completeness": fetched["source_completeness"]}
                        symbol_date = ",".join(r["symbol"] + "@" + r["date"] for r in rows)
                    elif path.endswith("/instock"):
                        day, symbol = request.get("date"), request.get("symbol")
                        if not isinstance(day, str) or not isinstance(symbol, str):
                            raise ResearchError("InStock接口需要日期及一个证券代码")
                        fetched = fetch_instock_daily(symbol, day)
                        rows, source_id = [fetched["row"]], fetched["source_id"]
                        volume_unit, amount_unit = "source_native", "source_native"
                        unit_basis = "InStock附带Eastmoney读取器未独立确认成交量/额单位"
                        origin = {"provider_response_sha256": fetched["source_sha256"],
                                  "source_completeness": fetched["source_completeness"]}
                        symbol_date = rows[0]["symbol"] + "@" + rows[0]["date"]
                    else:
                        day, symbol = request.get("date"), request.get("symbol")
                        count = request.get("count", 250)
                        if not isinstance(day, str) or not isinstance(symbol, str):
                            raise ResearchError("easy-tdx接口需要截止日期和一个证券代码")
                        fetched = fetch_easy_tdx_daily(symbol, day, count)
                        rows, source_id = fetched["rows"], fetched["source_id"]
                        volume_unit, amount_unit = "shares", "yuan"
                        unit_basis = "easy-tdx SecurityBar模型声明vol为股、amount为元；仅作来源声明，未独立认证"
                        origin = {"provider_response_sha256": fetched["source_sha256"],
                                  "source_completeness": fetched["source_completeness"],
                                  "response_representation": "canonical_decoded_rows_not_wire_packet"}
                        symbol_date = ",".join(r["symbol"] + "@" + r["date"] for r in rows)
                    csv_content = rows_to_csv(rows)
                    snapshot = import_csv(store, csv_content, {"source_id": source_id,
                        "source_ref": fetched["url"], "price_basis": "raw",
                        "volume_unit": volume_unit, "amount_unit": amount_unit,
                        "unit_basis": unit_basis, "record_mode": "manual_observation_snapshot",
                        "captured_at": fetched["captured_at"], "origin": origin})
                    capture = store.retain_source_capture(source_id=source_id, url=fetched["url"],
                        raw=fetched["raw"], captured_at=fetched["captured_at"],
                        source_sha256=fetched["source_sha256"], row_count=len(rows), symbol_date=symbol_date)
                    result = {"snapshot": quality(snapshot), "capture_id": capture["record_id"],
                              "source_sha256": fetched["source_sha256"],
                              "qualification": "来源与本机完整性已留档；来源质量/PIT仍未认证"}
                elif path == "/api/external-note":
                    record = add_external_note(store, request)
                    result = {"record_id": record["record_id"],
                              "metadata": external_note_summary(record),
                              "qualification": record["payload"]["qualification"]}
                elif path == "/api/analyze":
                    result = analysis(store, request)
                elif path == "/api/market-overview":
                    result = market_overview(store, request)
                elif path == "/api/import":
                    result = import_csv(store, request["csv"].encode("utf-8"), request["metadata"])
                    result = quality(result)
                elif path == "/api/card":
                    result = freeze_card(store, request)
                    result = {"record_id": result["record_id"], "record_hash": result["record_hash"]}
                elif path == "/api/candidate":
                    result = register_candidate(store, request)
                elif path == "/api/transition":
                    result = transition_candidate(store, request)
                elif path == "/api/risk":
                    result = record_risk(store, request)
                elif path == "/api/order-plan":
                    result = save_order_plan(store, request)
                elif path == "/api/protocol":
                    result = freeze_protocol(store, request)
                else:
                    self.send_json(404, {"error": "路径不存在"})
                    return
                self.send_json(200, result)
            except (ResearchError, KeyError, TypeError, ValueError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception:
                self.send_json(500, {"error": "操作失败；请用命令行doctor检查本地数据"})
            finally:
                store.close()

    try:
        httpd = HTTPServer(("127.0.0.1", port), Handler)
        httpd.timeout = 1
        httpd.socket.settimeout(15)
    except OSError as exc:
        raise ResearchError("本地端口无法启动；可用--port更换端口: " + str(exc)) from exc
    print(f"工作台已启动：http://127.0.0.1:{port}", flush=True)
    print("数据保存在：" + str(Path(directory).resolve()), flush=True)
    print("Ctrl+C关闭；这是描述性研究工作台，绩效验证与真实Shadow尚未通过。", flush=True)
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
