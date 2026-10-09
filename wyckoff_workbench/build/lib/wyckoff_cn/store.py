import json
import sqlite3
import uuid
from pathlib import Path

from .common import ResearchError, canonical, digest, now


class Store:
    """One immutable event ledger; all read models are derived from this ledger.

    Local hash chaining detects accidental changes. It is not a remote timestamp
    or protection against someone replacing the entire database and its hashes.
    """

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.raw = self.directory / "raw"
        self.raw.mkdir(exist_ok=True)
        self.connection = sqlite3.connect(self.directory / "research.sqlite3", timeout=20)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS ledger (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          record_id TEXT NOT NULL UNIQUE,
          kind TEXT NOT NULL,
          recorded_at TEXT NOT NULL,
          payload TEXT NOT NULL,
          previous_hash TEXT NOT NULL,
          record_hash TEXT NOT NULL UNIQUE
        );
        CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
        BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
        CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
        BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
        """)

    def close(self):
        self.connection.close()

    def records(self, kind=None):
        if kind:
            rows = self.connection.execute("SELECT * FROM ledger WHERE kind=? ORDER BY seq", (kind,))
        else:
            rows = self.connection.execute("SELECT * FROM ledger ORDER BY seq")
        return [dict(row, payload=json.loads(row["payload"])) for row in rows]

    def get(self, record_id, kind=None):
        row = self.connection.execute("SELECT * FROM ledger WHERE record_id=?", (record_id,)).fetchone()
        if row is None or (kind and row["kind"] != kind):
            raise ResearchError("引用不存在或类型不符: " + str(record_id))
        return dict(row, payload=json.loads(row["payload"]))

    def append(self, kind, payload, record_id=None):
        record_id = record_id or kind + "-" + uuid.uuid4().hex[:16]
        stamp = now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute("SELECT record_hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
            previous = row[0] if row else "0" * 64
            content = {"record_id": record_id, "kind": kind, "recorded_at": stamp,
                       "payload": payload, "previous_hash": previous}
            sha = digest(content)
            self.connection.execute("INSERT INTO ledger(record_id,kind,recorded_at,payload,previous_hash,record_hash) VALUES(?,?,?,?,?,?)",
                                    (record_id, kind, stamp, canonical(payload), previous, sha))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get(record_id)

    def verify(self):
        previous = "0" * 64
        errors = []
        for record in self.records():
            content = {key: record[key] for key in
                       ("record_id", "kind", "recorded_at", "payload", "previous_hash")}
            if record["previous_hash"] != previous or digest(content) != record["record_hash"]:
                errors.append("账本哈希不一致: " + record["record_id"])
            previous = record["record_hash"]
        for record in self.records("snapshot"):
            p = self.raw / (record["payload"]["source_sha256"] + ".bin")
            import hashlib
            if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != record["payload"]["source_sha256"]:
                errors.append("原始文件缺失或已改变: " + record["record_id"])
        for record in self.records("source_capture"):
            p = self.raw / (record["payload"]["source_sha256"] + ".bin")
            import hashlib
            if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != record["payload"]["source_sha256"]:
                errors.append("来源响应缺失或已改变: " + record["record_id"])
        for record in self.records("external_note"):
            p = self.raw / ("external-" + record["payload"]["content_sha256"] + ".txt")
            import hashlib
            if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != record["payload"]["content_sha256"]:
                errors.append("外部输出原文缺失或已改变: " + record["record_id"])
        return {"ok": not errors, "record_count": len(self.records()), "errors": errors,
                "scope": "local_integrity_only"}

    def retain_source_capture(self, *, source_id, url, raw, captured_at, source_sha256, row_count, symbol_date):
        import hashlib
        actual = hashlib.sha256(raw).hexdigest()
        if actual != source_sha256:
            raise ResearchError("来源响应哈希不一致")
        path = self.raw / (actual + ".bin")
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != actual:
            raise ResearchError("同哈希来源文件已存在但内容不符")
        if not path.exists():
            path.write_bytes(raw)
        payload = {"source_id": source_id, "source_ref": url, "captured_at": captured_at,
                   "source_sha256": actual, "retained_file": "raw/" + actual + ".bin",
                   "row_count": row_count, "symbol_date": symbol_date,
                   "scope": "raw_provider_response_retained_locally_not_independent_source_certification"}
        return self.append("source_capture", payload)
