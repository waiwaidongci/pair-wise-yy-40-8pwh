from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import BATCH_ENTITY, ENTITY, ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS report_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','confirmed','failed')),
                    created_by TEXT NOT NULL,
                    confirmed_by TEXT,
                    receipt_no TEXT,
                    diff TEXT NOT NULL DEFAULT '[]',
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS report_batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL
                        REFERENCES report_batches(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    external_ref TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    local_version INTEGER NOT NULL,
                    receipt_version INTEGER,
                    receipt_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(receipt_status IN ('pending','matched','diff','failed')),
                    diff TEXT NOT NULL DEFAULT '[]',
                    stale INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, item_id)
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def _insert_audit(self, cur: sqlite3.Cursor, action: str, entity_type: str,
                      entity_id: int, actor: str, detail: dict) -> Dict[str, Any]:
        """在调用方事务内写入审计事件，保证状态变更与审计链同生共死。"""
        row = cur.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous = row["entry_hash"] if row else "GENESIS"
        event = make_entry(action, entity_type, entity_id, actor, detail, previous)
        cur.execute(
            """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
               previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (event["action"], event["entity_type"], event["entity_id"], event["actor"],
             json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
             event["previous_hash"], event["entry_hash"], event["created_at"]),
        )
        return event

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, audit_detail: dict) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
                self._insert_audit(cur, "create", ENTITY, item_id, actor, audit_detail)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str, audit_detail: dict) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
            self._insert_audit(cur, "transition", ENTITY, item_id, actor, audit_detail)
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str,
                   audit_detail: dict) -> tuple:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
                # 证据更新：已确认批次中该项目的上报结论失效，需重算后重新上报
                stale_rows = self.conn.execute(
                    """SELECT batch_id FROM report_batch_items
                       WHERE item_id=? AND stale=0
                         AND batch_id IN (SELECT id FROM report_batches WHERE status='confirmed')""",
                    (item_id,),
                ).fetchall()
                invalidated = [int(r["batch_id"]) for r in stale_rows]
                if invalidated:
                    self.conn.execute(
                        """UPDATE report_batch_items SET stale=1
                           WHERE item_id=? AND stale=0
                             AND batch_id IN (SELECT id FROM report_batches WHERE status='confirmed')""",
                        (item_id,),
                    )
                audit_detail = dict(audit_detail, invalidated_batches=invalidated)
                self._insert_audit(cur, "record", ENTITY, item_id, actor, audit_detail)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row), invalidated

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---------- 上报批次 ----------

    def create_batch(self, batch_no: str, created_by: str,
                     items: List[tuple]) -> Dict[str, Any]:
        """items: [(item_id, external_ref, snapshot_dict, local_version), ...]

        批次主表、条目与审计链在同一事务写入。
        """
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO report_batches(batch_no, status, created_by, created_at)
                   VALUES(?, 'pending', ?, ?)""",
                (batch_no, created_by, now),
            )
            batch_id = int(cur.lastrowid)
            for item_id, external_ref, snapshot, local_version in items:
                self.conn.execute(
                    """INSERT INTO report_batch_items(batch_id, item_id, external_ref,
                       snapshot, local_version, created_at) VALUES(?,?,?,?,?,?)""",
                    (batch_id, item_id, external_ref,
                     json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                     local_version, now),
                )
            self._insert_audit(cur, "batch_created", BATCH_ENTITY, batch_id, created_by,
                               {"batch_no": batch_no, "items": [i[0] for i in items]})
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM report_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return self._load_batch(row)

    def _load_batch(self, row: sqlite3.Row) -> Dict[str, Any]:
        batch = dict(row)
        batch["diff"] = json.loads(batch["diff"])
        batch["items"] = []
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM report_batch_items WHERE batch_id=? ORDER BY id",
                (batch["id"],)).fetchall()
        for r in rows:
            it = dict(r)
            it["snapshot"] = json.loads(it["snapshot"])
            it["diff"] = json.loads(it["diff"])
            it["stale"] = bool(it["stale"])
            batch["items"].append(it)
        return batch

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM report_batches ORDER BY id DESC").fetchall()
        result = []
        for row in rows:
            b = dict(row)
            b["diff"] = json.loads(b["diff"])
            result.append(b)
        return result

    def mark_batch_failed(self, batch_id: int, error: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE report_batches SET status='failed', last_error=? WHERE id=?",
                (error, batch_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("批次不存在")
            self._insert_audit(cur, "batch_failed", BATCH_ENTITY, batch_id, actor,
                               {"error": error})
        return self.get_batch(batch_id)

    def mark_batch_confirmed(self, batch_id: int, actor: str, receipt_no: str,
                             diff: list, item_results: List[tuple]) -> Dict[str, Any]:
        """item_results: [(item_id, receipt_version, receipt_status, item_diff), ...]

        批次状态、条目对账结果与审计链在同一事务写入。
        """
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE report_batches SET status='confirmed', confirmed_by=?,
                   receipt_no=?, diff=?, confirmed_at=? WHERE id=?""",
                (actor, receipt_no, json.dumps(diff, ensure_ascii=False), now, batch_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("批次不存在")
            for item_id, receipt_version, receipt_status, item_diff in item_results:
                self.conn.execute(
                    """UPDATE report_batch_items SET receipt_version=?, receipt_status=?, diff=?
                       WHERE batch_id=? AND item_id=?""",
                    (receipt_version, receipt_status,
                     json.dumps(item_diff, ensure_ascii=False), batch_id, item_id),
                )
            self._insert_audit(cur, "batch_confirmed", BATCH_ENTITY, batch_id, actor,
                               {"receipt_no": receipt_no,
                                "matched": sum(1 for r in item_results if r[2] == "matched"),
                                "diff_count": sum(1 for r in item_results if r[2] == "diff")})
        return self.get_batch(batch_id)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
