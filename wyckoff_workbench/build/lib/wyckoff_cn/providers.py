"""Read-only daily-bar connectors derived from the two user-provided projects.

They return source observations only. They never create strategy signals or orders.
Network access is explicit (user click or CLI command) and each result is retained
as its own immutable snapshot; no silent provider fallback or source merging.
"""
import csv
import io
import json
import math
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from datetime import date, datetime

from .common import ResearchError, SHANGHAI, iso_date, now

TDX_PACKAGE_URL = "https://www.tdx.com.cn/products/data/data/g4day/{ymd}.zip"
EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
MAX_PACKAGE_BYTES = 12 * 1024 * 1024
MAX_EASTMONEY_BYTES = 8 * 1024 * 1024
TDX_BJ_FIRST_DAY = "20220506"
# Floors documented by the supplied a-stock-data source. They detect obviously
# truncated market files; they do not certify exchange-level universe coverage.
TDX_MIN_PRICED = {"sh": 10000, "sz": 3000, "bj": 50}
_A_SHARE_PREFIXES = {
    "sh": ("600", "601", "603", "605", "688", "689"),
    "sz": ("000", "001", "002", "003", "300", "301", "302"),
    "bj": ("4", "8", "92"),
}
_last_instock_fetch = 0.0
_instock_interval = 5.0


def normalize_stock_symbol(symbol):
    if not isinstance(symbol, str):
        raise ResearchError("证券代码必须是字符串")
    value = symbol.strip().lower()
    m = re.fullmatch(r"(sh|sz|bj)([0-9]{6})", value)
    if m:
        market, code = m.group(1), m.group(2)
    elif re.fullmatch(r"[0-9]{6}", value):
        code = value
        if code.startswith(("600", "601", "603", "605", "688", "689")):
            market = "sh"
        elif code.startswith(("000", "001", "002", "003", "300", "301", "302")):
            market = "sz"
        elif code.startswith(("4", "8", "92")):
            market = "bj"
        else:
            raise ResearchError("代码市场无法无歧义识别；请提供sh/sz/bj前缀")
    else:
        raise ResearchError("代码需为6位数字，可带sh/sz/bj市场前缀")
    ordinary = ((market == "sh" and code.startswith(("600", "601", "603", "605", "688", "689"))) or
                (market == "sz" and code.startswith(("000", "001", "002", "003", "300", "301", "302"))) or
                (market == "bj" and code.startswith(("4", "8", "92"))))
    if not ordinary:
        raise ResearchError("本连接器仅接受普通A股代码；指数、基金、债券和其它品种需单独定义数据口径")
    return market + code, market, code


def _http_bytes(url, *, max_bytes, timeout=(8, 30), headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 WyckoffResearch/0.4",
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout[1]) as response:
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ResearchError("来源响应超过安全大小上限")
            return raw, response.geturl(), response.status
    except ResearchError:
        raise
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise ResearchError("来源请求失败；本次没有导入数据: " + type(exc).__name__) from exc


def _finite(value, name):
    if isinstance(value, bool):
        raise ResearchError(name + "来源值类型错误")
    try:
        n = float(value)
    except (ValueError, TypeError) as exc:
        raise ResearchError(name + "不是数值") from exc
    if not math.isfinite(n):
        raise ResearchError(name + "不是有限数值")
    return n


def parse_tdx_daily_package(raw, ymd, wanted_symbols):
    """Strictly parse selected equity rows from the user's a-stock-data source.

    File layout and field offsets follow upstream a-stock-data §1.3. This parser
    validates archive structure and per-market code/block consistency, but does
    not claim the provider's universe-completeness thresholds were independently
    certified here.
    """
    d = iso_date(ymd, "行情日期")
    wanted = {normalize_stock_symbol(x)[0] for x in wanted_symbols}
    if not wanted:
        raise ResearchError("至少指定一只普通A股")
    found = {}
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        names = set(archive.namelist())
        if len(names) != len(archive.namelist()):
            raise ResearchError("盘后包含重复文件名")
        if sum(info.file_size for info in archive.infolist()) > 128 * 1024 * 1024:
            raise ResearchError("盘后解压后超过安全大小上限")
        for market in ("sh", "sz", "bj"):
            package_day = d.strftime("%y%m%d")
            code_name, bar_name = f"{market}{package_day}.cod", f"{market}{package_day}.md1"
            if market == "bj" and ymd.replace("-", "") < "20220506" and code_name not in names and bar_name not in names:
                continue
            if code_name not in names or bar_name not in names:
                raise ResearchError(f"盘后包缺少{market.upper()}行情文件")
            codes, blocks = archive.read(code_name), archive.read(bar_name)
            if len(codes) % 150 or len(blocks) % 512 or len(codes) // 150 != len(blocks) // 512:
                raise ResearchError(f"{market.upper()}代码表与行情块长度不匹配")
            seen_codes, seen_seq = set(), set()
            for offset in range(0, len(codes), 150):
                rec = codes[offset:offset + 150]
                try:
                    code = rec[:6].rstrip(b"\x00 ").decode("ascii")
                except UnicodeDecodeError as exc:
                    raise ResearchError("盘后包出现非ASCII证券代码") from exc
                seq = struct.unpack("<H", rec[32:34])[0]
                if not re.fullmatch(r"[0-9]{6}", code) or code in seen_codes or seq in seen_seq:
                    raise ResearchError(f"{market.upper()}代码/行情块序号非法或重复")
                seen_codes.add(code)
                seen_seq.add(seq)
                symbol = market + code
                if symbol not in wanted:
                    continue
                block = blocks[seq * 512:(seq + 1) * 512]
                if len(block) != 512:
                    raise ResearchError(f"{symbol}行情块越界")
                previous = struct.unpack("<d", block[4:12])[0]
                open_, high, low, close = struct.unpack("<4d", block[12:44])
                amount = struct.unpack("<d", block[72:80])[0]
                volume = struct.unpack("<Q", block[56:64])[0]
                vals = (previous, open_, high, low, close, amount)
                if not all(math.isfinite(v) for v in vals):
                    raise ResearchError(f"{symbol}含非有限价格/成交额")
                if close <= 0:
                    raise ResearchError(f"{symbol}当日无有价记录；原因未知，不标为停牌或缺失")
                if min(open_, high, low, close) <= 0 or not low <= min(open_, close) <= max(open_, close) <= high or amount < 0:
                    raise ResearchError(f"{symbol}的OHLC/成交额不合法")
                try:
                    name = rec[40:72].split(b"\x00")[0].decode("gbk").strip()
                except UnicodeDecodeError as exc:
                    raise ResearchError(f"{symbol}证券名称编码异常") from exc
                if not name:
                    raise ResearchError(f"{symbol}缺少证券名称")
                found[symbol] = {"symbol": symbol, "date": d.isoformat(), "open": open_,
                    "high": high, "low": low, "close": close, "volume": volume,
                    "amount": amount, "complete": "true", "status": "unknown",
                    "source_available_at": "", "availability_basis": "unknown",
                    "source_revision_id": "unknown", "name": name,
                    "previous_close": previous, "volume_unit": "shares", "amount_unit": "yuan"}
        archive.close()
    except ResearchError:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, struct.error, KeyError, OSError) as exc:
        raise ResearchError("盘后包格式错误或下载损坏: " + type(exc).__name__) from exc
    missing = wanted - found.keys()
    if missing:
        raise ResearchError("盘后包未找到指定证券: " + ", ".join(sorted(missing)) + "；不自动回退到其他来源")
    return [found[s] for s in sorted(wanted)]


def parse_tdx_market_package(raw, ymd, minimum_priced=None):
    """Parse all priced ordinary A shares from a TDX end-of-day package.

    This is an observed package universe, not an exchange-certified or
    point-in-time universe. Zero-close ordinary shares are reported separately
    as unpriced; the parser does not infer suspension, listing status, or ST.
    ``minimum_priced`` exists for deterministic parser fixtures only.
    """
    d = iso_date(ymd, "行情日期")
    ymd_compact = d.strftime("%Y%m%d")
    floors = dict(TDX_MIN_PRICED if minimum_priced is None else minimum_priced)
    if set(floors) != {"sh", "sz", "bj"} or any(type(v) is not int or v < 1 for v in floors.values()):
        raise ResearchError("TDX市场包最低有价行阈值不合法")
    rows, unpriced = [], []
    market_counts = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ResearchError("盘后包含重复文件名")
            if sum(info.file_size for info in infos) > 128 * 1024 * 1024:
                raise ResearchError("盘后解压后超过安全大小上限")
            name_set = set(names)
            for market in ("sh", "sz", "bj"):
                code_name = f"{market}{d.strftime('%y%m%d')}.cod"
                bar_name = f"{market}{d.strftime('%y%m%d')}.md1"
                if market == "bj" and ymd_compact < TDX_BJ_FIRST_DAY and code_name not in name_set and bar_name not in name_set:
                    market_counts[market] = {"package_present": False, "code_rows": 0,
                        "a_share_codes": 0, "priced_package_rows": 0, "priced_a_shares": 0,
                        "unpriced_a_shares": 0, "minimum_priced_floor": floors[market],
                        "floor_status": "not_applicable_before_bse_package_start"}
                    continue
                if code_name not in name_set or bar_name not in name_set:
                    raise ResearchError(f"盘后包缺少{market.upper()}行情文件")
                codes, blocks = archive.read(code_name), archive.read(bar_name)
                if len(codes) % 150 or len(blocks) % 512 or len(codes) // 150 != len(blocks) // 512:
                    raise ResearchError(f"{market.upper()}代码表与行情块长度不匹配")
                count = len(codes) // 150
                seen_codes, seen_seq = set(), set()
                priced_package_rows = a_share_codes = priced_a_shares = unpriced_a_shares = 0
                for offset in range(0, len(codes), 150):
                    rec = codes[offset:offset + 150]
                    try:
                        code = rec[:6].rstrip(b"\x00 ").decode("ascii")
                    except UnicodeDecodeError as exc:
                        raise ResearchError("盘后包出现非ASCII证券代码") from exc
                    seq = struct.unpack("<H", rec[32:34])[0]
                    if not re.fullmatch(r"[0-9]{6}", code) or code in seen_codes or seq in seen_seq:
                        raise ResearchError(f"{market.upper()}代码/行情块序号非法或重复")
                    if seq >= count:
                        raise ResearchError(f"{market.upper()}行情块序号越界")
                    seen_codes.add(code)
                    seen_seq.add(seq)
                    block = blocks[seq * 512:(seq + 1) * 512]
                    if len(block) != 512:
                        raise ResearchError(f"{market.upper()}{code}行情块越界")
                    previous = struct.unpack("<d", block[4:12])[0]
                    open_, high, low, close = struct.unpack("<4d", block[12:44])
                    amount = struct.unpack("<d", block[72:80])[0]
                    if not all(math.isfinite(v) for v in (previous, open_, high, low, close, amount)):
                        raise ResearchError(f"{market.upper()}{code}含非有限价格/成交额")
                    is_a_share = code.startswith(_A_SHARE_PREFIXES[market])
                    if close > 0:
                        priced_package_rows += 1
                    if not is_a_share:
                        continue
                    a_share_codes += 1
                    try:
                        name = rec[40:72].split(b"\x00")[0].decode("gbk").strip()
                    except UnicodeDecodeError as exc:
                        raise ResearchError(f"{market.upper()}{code}名称编码异常") from exc
                    if not name:
                        raise ResearchError(f"{market.upper()}{code}缺少证券名称")
                    symbol = market + code
                    if close <= 0:
                        if close < 0 or min(previous, open_, high, low, amount) < 0:
                            raise ResearchError(f"{symbol}无价记录包含负值")
                        unpriced.append({"symbol": symbol, "name": name,
                            "reason": "no_positive_close_in_package", "status": "unknown"})
                        unpriced_a_shares += 1
                        continue
                    volume = struct.unpack("<Q", block[56:64])[0]
                    if min(open_, high, low, close) <= 0 or not low <= min(open_, close) <= max(open_, close) <= high or amount < 0:
                        raise ResearchError(f"{symbol}的OHLC/成交额不合法")
                    rows.append({"symbol": symbol, "date": d.isoformat(), "open": open_,
                        "high": high, "low": low, "close": close, "volume": volume,
                        "amount": amount, "complete": "true", "status": "unknown",
                        "source_available_at": "", "availability_basis": "unknown",
                        "source_revision_id": "unknown", "previous_close": previous if previous > 0 else None,
                        "volume_unit": "shares", "amount_unit": "yuan", "name": name})
                    priced_a_shares += 1
                if seen_seq != set(range(count)):
                    raise ResearchError(f"{market.upper()}代码表与行情块序号不是完整的一一映射")
                if priced_package_rows < floors[market]:
                    raise ResearchError(f"{market.upper()}市场只有{priced_package_rows}条有价行情，低于来源完整包下限{floors[market]}")
                if a_share_codes == 0 or priced_a_shares == 0:
                    raise ResearchError(f"{market.upper()}市场没有可核验的有价普通A股代码")
                market_counts[market] = {"package_present": True, "code_rows": count,
                    "a_share_codes": a_share_codes, "priced_package_rows": priced_package_rows,
                    "priced_a_shares": priced_a_shares, "unpriced_a_shares": unpriced_a_shares,
                    "minimum_priced_floor": floors[market], "floor_status": "passed"}
    except ResearchError:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, struct.error, KeyError, OSError) as exc:
        raise ResearchError("盘后包格式错误或下载损坏: " + type(exc).__name__) from exc
    if not rows:
        raise ResearchError("盘后包没有可导入的有价普通A股")
    rows.sort(key=lambda row: row["symbol"])
    package_markets = [m for m, values in market_counts.items() if values["package_present"]]
    all_three_markets = package_markets == ["sh", "sz", "bj"]
    return {"rows": rows, "unpriced_a_shares": sorted(unpriced, key=lambda r: r["symbol"]),
        "market_counts": market_counts, "market_scope": package_markets,
        "sse_szse_bse_package_present": all_three_markets,
        "priced_a_share_count": len(rows), "unpriced_a_share_count": len(unpriced),
        "a_share_code_count": len(rows) + len(unpriced),
        "provider_completeness": "structure_and_source_floor_checked_universe_not_exchange_certified",
        "status_coverage": "unknown"}


def fetch_tdx_market(ymd):
    """Fetch one explicit TDX end-of-day package; never switches data source."""
    d = iso_date(ymd, "行情日期")
    if d > now_date_shanghai():
        raise ResearchError("不能请求未来行情日期")
    url = TDX_PACKAGE_URL.format(ymd=d.strftime("%Y%m%d"))
    raw, final_url, status = _http_bytes(url, max_bytes=MAX_PACKAGE_BYTES, timeout=(10, 90))
    if not raw.startswith(b"PK"):
        raise ResearchError("来源未返回ZIP盘后包；可能是非交易日、未发布或错误页")
    parsed = parse_tdx_market_package(raw, d.isoformat())
    return {**parsed, "raw": raw, "url": final_url,
        "source_id": "a-stock-data-3.10.1/tdx_daily_package/a_share_observation",
        "captured_at": now(), "source_sha256": __import__("hashlib").sha256(raw).hexdigest(),
        "record_mode": "manual_observation_snapshot"}


def fetch_tdx_daily(ymd, wanted_symbols):
    d = iso_date(ymd, "行情日期")
    if d > now_date_shanghai():
        raise ResearchError("不能请求未来行情日期")
    url = TDX_PACKAGE_URL.format(ymd=d.strftime("%Y%m%d"))
    raw, final_url, status = _http_bytes(url, max_bytes=MAX_PACKAGE_BYTES, timeout=(10, 90))
    if not raw.startswith(b"PK"):
        raise ResearchError("来源未返回ZIP盘后包；可能是非交易日、未发布或错误页")
    rows = parse_tdx_daily_package(raw, d.isoformat(), wanted_symbols)
    return {"rows": rows, "raw": raw, "url": final_url, "source_id": "a-stock-data-3.10.1/tdx_daily_package",
            "captured_at": now(), "source_sha256": __import__("hashlib").sha256(raw).hexdigest(),
            "source_completeness": "package_structure_checked_universe_completeness_not_independently_certified",
            "record_mode": "manual_observation_snapshot"}


def parse_instock_eastmoney_response(raw, symbol, ymd):
    full, market, code = normalize_stock_symbol(symbol)
    iso_date(ymd, "行情日期")
    if market == "bj":
        raise ResearchError("InStock附带抓取器不支持北交所；未调用来源")
    try:
        root = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchError("InStock来源响应不是有效UTF-8 JSON") from exc
    if not isinstance(root, dict) or "data" not in root:
        raise ResearchError("InStock来源响应结构不符")
    data = root["data"]
    if data is None:
        raise ResearchError("InStock来源无数据或接口未发布；不能解释为该证券没有行情")
    if not isinstance(data, dict) or not isinstance(data.get("klines"), list):
        raise ResearchError("InStock来源K线字段缺失或类型改变")
    matches = []
    for item in data["klines"]:
        if not isinstance(item, str):
            raise ResearchError("InStock来源K线行不是字符串")
        parts = item.split(",")
        if len(parts) < 7:
            raise ResearchError("InStock来源K线字段不足")
        if parts[0] != ymd:
            continue
        o, c, h, l, v, amount = (_finite(parts[i], f"InStock字段{i}") for i in range(1, 7))
        if min(o, c, h, l) <= 0 or not l <= min(o, c) <= max(o, c) <= h or v < 0 or amount < 0:
            raise ResearchError("InStock来源OHLC/成交额不合法")
        matches.append({"symbol": full, "date": ymd, "open": o, "close": c,
                        "high": h, "low": l, "volume": v, "amount": amount,
                        "complete": "unknown", "status": "unknown",
                        "source_available_at": "", "availability_basis": "unknown",
                        "source_revision_id": "unknown"})
    if len(matches) != 1:
        raise ResearchError(f"InStock来源{ymd}行数为{len(matches)}；要求唯一一行，未导入")
    return matches[0]


def fetch_instock_daily(symbol, ymd):
    global _last_instock_fetch
    full, market, code = normalize_stock_symbol(symbol)
    if market == "bj":
        raise ResearchError("InStock附带抓取器不支持北交所")
    day = iso_date(ymd, "行情日期")
    if day > now_date_shanghai():
        raise ResearchError("不能请求未来行情日期")
    # Preserve the upstream InStock Eastmoney endpoint and daily/fq=raw settings;
    # serialize calls to avoid burst requests. This is observation-only.
    wait = _instock_interval - (time.monotonic() - _last_instock_fetch)
    if wait > 0:
        time.sleep(wait)
    market_id = 1 if market == "sh" else 0
    query = urllib.parse.urlencode({"fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
        "ut": "7eea3edcaed734bea9cbfc24409ed989", "klt": "101", "fqt": "0",
        "secid": f"{market_id}.{code}", "beg": day.strftime("%Y%m%d"),
        "end": day.strftime("%Y%m%d")})
    url = EASTMONEY_KLINE_URL + "?" + query
    raw, final_url, status = _http_bytes(url, max_bytes=MAX_EASTMONEY_BYTES, timeout=(8, 20))
    _last_instock_fetch = time.monotonic()
    row = parse_instock_eastmoney_response(raw, full, day.isoformat())
    return {"row": row, "raw": raw, "url": final_url, "source_id": "instock-master/stock_zh_a_hist-readonly",
            "captured_at": now(), "source_sha256": __import__("hashlib").sha256(raw).hexdigest(),
            "source_completeness": "single_day_row_present_unit_and_pit_not_independently_certified",
            "record_mode": "manual_observation_snapshot", "volume_unit": "source_native_unknown"}


def normalize_easy_tdx_daily_frame(frame, symbol, as_of, captured_at):
    """Convert easy-tdx daily bars into an auditable observation snapshot.

    Only public daily-bar fields are used. Today's bar is incomplete because
    the provider does not certify that the session has closed.
    """
    full, market, code = normalize_stock_symbol(symbol)
    try:
        cutoff = iso_date(as_of, "观察截止日")
    except ResearchError:
        raise
    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ResearchError("easy-tdx日期或采集时间格式错误") from exc
    if captured.tzinfo is None:
        raise ResearchError("easy-tdx采集时间必须含时区")
    captured_day = captured.astimezone(SHANGHAI).date()
    if cutoff > captured_day:
        raise ResearchError("截止日不能晚于采集时的上海日期")
    required = {"date", "open", "high", "low", "close", "vol", "amount"}
    if not hasattr(frame, "columns") or not required.issubset(set(frame.columns)):
        raise ResearchError("easy-tdx日线字段缺失或接口已变化")
    selected = []
    for record in frame.to_dict(orient="records"):
        raw_day = record["date"]
        try:
            day = raw_day.date().isoformat() if hasattr(raw_day, "date") else str(raw_day)[:10]
            parsed_day = iso_date(day, "easy-tdx行情日期")
        except (ValueError, TypeError, ResearchError) as exc:
            raise ResearchError("easy-tdx返回无效日线日期") from exc
        if parsed_day > cutoff:
            continue
        values = {name: _finite(record[name], "easy-tdx/" + name)
                  for name in ("open", "high", "low", "close", "vol", "amount")}
        o, h, l, c, volume, amount = (values[k] for k in
                                      ("open", "high", "low", "close", "vol", "amount"))
        if min(o, h, l, c) <= 0 or not l <= min(o, c) <= max(o, c) <= h or volume < 0 or amount < 0:
            raise ResearchError("easy-tdx返回不合法OHLC/量额")
        selected.append({"symbol": full, "date": day, "open": o, "high": h,
            "low": l, "close": c, "volume": volume, "amount": amount,
            "complete": "false" if parsed_day == captured_day else "true",
            "status": "unknown", "source_available_at": "", "availability_basis": "unknown",
            "source_revision_id": "unknown", "volume_unit": "shares", "amount_unit": "yuan"})
    selected.sort(key=lambda item: item["date"])
    days = [item["date"] for item in selected]
    if not selected:
        raise ResearchError("easy-tdx没有返回截止日及之前的日线")
    if len(days) != len(set(days)):
        raise ResearchError("easy-tdx返回重复日期；拒绝静默去重")
    return selected


def fetch_easy_tdx_daily(symbol, as_of, count=800):
    """Fetch one explicitly requested security's daily bars through easy-tdx."""
    full, market, code = normalize_stock_symbol(symbol)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 800:
        raise ResearchError("easy-tdx根数须为1至800")
    try:
        from easy_tdx import KlineCategory, Market, TdxClient
    except ImportError as exc:
        raise ResearchError("easy-tdx缺少运行依赖；请安装工作台easy-tdx扩展") from exc
    try:
        # Use easy-tdx's configured/default host without latency sweeps or
        # automatic failover; the exact endpoint is retained with the capture.
        client = TdxClient(auto_reconnect=False)
        with client:
            frame = client.get_security_bars(
                Market.SH if market == "sh" else Market.SZ if market == "sz" else Market.BJ,
                code, KlineCategory.DAY, 0, count)
            host, port = client._host, client._port
        captured_at = now()
        rows = normalize_easy_tdx_daily_frame(frame, full, as_of, captured_at)
    except ResearchError:
        raise
    except Exception as exc:
        raise ResearchError("easy-tdx行情请求失败；未切换来源: " + type(exc).__name__) from exc
    # easy-tdx exposes decoded bars rather than the wire packet. Preserve its
    # canonical returned rows as the auditable response representation.
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    import hashlib
    return {"rows": rows, "raw": raw, "url": f"tdx-tcp://{host}:{port}",
        "source_id": "easy-tdx/1.20.8/security-bars-daily",
        "captured_at": captured_at, "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_completeness": "provider_bars_preserved; completeness_and_PIT_not_independently_certified",
        "record_mode": "manual_observation_snapshot"}


def now_date_shanghai():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone(SHANGHAI).date()


def rows_to_csv(rows):
    fields = ["symbol", "date", "open", "high", "low", "close", "volume", "amount",
              "complete", "status", "source_available_at", "availability_basis", "source_revision_id",
              "previous_close"]
    f = io.StringIO(newline="")
    writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows([{k: r.get(k, "") for k in fields} for r in rows])
    return f.getvalue().encode("utf-8")

