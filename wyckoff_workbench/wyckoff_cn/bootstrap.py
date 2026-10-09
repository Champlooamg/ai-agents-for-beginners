import csv
import hashlib
import io
import json
from pathlib import Path

from .common import ResearchError
from .data import import_csv
from .research import freeze_protocol


def bootstrap(store, project):
    """Import only archived, hash-checked source rows. No online fetching."""
    root = Path(project) / "原始资料_V0.2" / "个股研究" / "605366_2026-10-08"
    manifest = json.loads((root / "data_manifest.json").read_text(encoding="utf-8"))
    entries = {r["file"]: r for r in manifest}
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=["symbol", "date", "open", "high", "low", "close", "volume", "amount", "complete", "status", "source_available_at", "availability_basis"])
    writer.writeheader()
    counts = {}
    origin = []
    for filename in ("stock_raw.txt", "benchmark.txt", "market.txt"):
        raw = (root / filename).read_bytes()
        entry = entries[filename]
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ResearchError("原始附件哈希不匹配: " + filename)
        content = json.loads(raw)
        if content.get("code") != 0:
            raise ResearchError("来源快照包含接口错误")
        for symbol, values in content["data"].items():
            rows = values["day"]
            counts[symbol] = len(rows)
            for row in rows:
                # Tencent day rows: date, open, close, high, low, volume.
                writer.writerow({"symbol": symbol, "date": row[0], "open": row[1],
                                 "close": row[2], "high": row[3], "low": row[4], "volume": row[5],
                                 "amount": "", "complete": "true", "status": "unknown",
                                 "source_available_at": "", "availability_basis": "unknown"})
        origin.append({"file": filename, "sha256": entry["sha256"],
                       "fetched_at": entry["fetched_at"], "url": entry["url"]})
    snap = import_csv(store, buffer.getvalue().encode("utf-8"), {
        "source_id": "attached-tencent-archive-20261008",
        "source_ref": "原始资料_V0.2/个股研究/605366_2026-10-08/data_manifest.json",
        "record_mode": "manual_observation_snapshot", "price_basis": "raw",
        "captured_at": max(e["fetched_at"] for e in origin),
        "volume_unit": "source_native", "amount_unit": "unknown",
        "unit_basis": "附件verified_metrics记录个股为手；指数单位未独立验收，整体保留source_native",
        "origin": origin,
    })
    protocol = freeze_protocol(store)
    return {"snapshot_id": snap["record_id"], "protocol_id": protocol["record_id"],
            "row_counts": counts, "purpose": "archived_descriptive_example",
            "historical_pit": "not_certified", "fresh_data_fetched": False}
