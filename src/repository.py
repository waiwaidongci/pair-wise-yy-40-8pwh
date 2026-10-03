from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, UpstreamError, ValidationError
from .rules import (BATCH_DRAFT, BATCH_FAILED, BATCH_PREFIX, BATCH_SUBMITTED,
                    ENTRY_PENDING, ENTITY, ID_PREFIX, STATES, report_conclusion)


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
                    status TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    confirmed_by TEXT,
                    fail_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS report_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES report_batches(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    external_ref TEXT NOT NULL,
                    item_version INTEGER NOT NULL,
                    result TEXT NOT NULL,
                    receipt_version INTEGER,
                    diffs TEXT NOT NULL DEFAULT '[]',
                    reported_conclusion TEXT,
                    UNIQUE(batch_id, external_ref)
                );
            """)
            self._migrate_columns()

    def _migrate_columns(self) -> None:
        added = {
            "evidence_version": "INTEGER NOT NULL DEFAULT 0",
            "reported_version": "INTEGER",
            "reported_conclusion": "TEXT",
            "reported_batch_no": "TEXT",
            "reported_at": "TEXT",
            "report_stale": "INTEGER NOT NULL DEFAULT 0",
        }
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
        for name, decl in added.items():
            if name not in cols:
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {name} {decl}")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        raw = result.get("reported_conclusion")
        result["reported_conclusion"] = json.loads(raw) if raw else None
        result["report_stale"] = bool(result.get("report_stale"))
        return result

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
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
                        actor: str) -> Dict[str, Any]:
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
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            item_row = self.conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if item_row is None:
                raise NotFoundError("项目不存在")
            try:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("记录唯一标识已存在") from exc
            record_id = int(cur.lastrowid)
            # 证据更新：证据版本递增；原上报结论失效重算（项目版本一并递增用于台账对账）
            self.conn.execute(
                """UPDATE items SET evidence_version=evidence_version+1,
                   version=version+1, report_stale=CASE WHEN reported_version IS NOT NULL THEN 1 ELSE 0 END,
                   updated_at=? WHERE id=?""",
                (now, item_id),
            )
            self._append_audit_locked("record", ENTITY, item_id, actor, {
                "record_id": record_id, "kind": kind, "status": status,
                "evidence_version": item_row["evidence_version"] + 1,
            })
            if item_row["reported_version"] is not None:
                self._append_audit_locked("report_invalidated", ENTITY, item_id, actor, {
                    "record_id": record_id, "batch_no": item_row["reported_batch_no"],
                    "reported_version": item_row["reported_version"],
                })
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)
            ).fetchone()
        return dict(row)

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

    def open_record_counts(self, item_ids: List[int]) -> Dict[int, int]:
        if not item_ids:
            return {}
        marks = ",".join("?" for _ in item_ids)
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT item_id, SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS n
                    FROM records WHERE item_id IN ({marks}) GROUP BY item_id""",
                tuple(item_ids),
            ).fetchall()
        return {int(r["item_id"]): int(r["n"] or 0) for r in rows}

    # --- 上报批次 ---------------------------------------------------------
    @staticmethod
    def _entry(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["diffs"] = json.loads(item["diffs"])
        item["reported_conclusion"] = (
            json.loads(item["reported_conclusion"]) if item["reported_conclusion"] else None)
        return item

    @staticmethod
    def _batch(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_report_batch(self, external_refs: List[str], actor: str
                            ) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            # 锁定条目顺序，防止两个批次并发纳入同一外部编号
            refs = list(dict.fromkeys(external_refs))
            marks = ",".join("?" for _ in refs)
            rows = self.conn.execute(
                f"SELECT * FROM items WHERE external_ref IN ({marks})", tuple(refs)
            ).fetchall()
            by_ref = {r["external_ref"]: self._item(r) for r in rows}
            missing = [r for r in refs if r not in by_ref]
            if missing:
                raise ValidationError(f"外部编号不存在: {','.join(missing)}")
            locked = self.conn.execute(
                """SELECT e.external_ref FROM report_entries e
                   JOIN report_batches b ON b.id=e.batch_id
                   WHERE e.external_ref IN ({}) AND b.status IN (?,?)""".format(marks),
                tuple(refs) + (BATCH_DRAFT, BATCH_FAILED),
            ).fetchall()
            if locked:
                raise ConflictError(
                    "外部编号已在未完成批次中: " + ",".join(r["external_ref"] for r in locked))
            batch_no = self._next_batch_no_locked()
            cur = self.conn.execute(
                """INSERT INTO report_batches(batch_no,status,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (batch_no, BATCH_DRAFT, actor, now, now),
            )
            batch_id = int(cur.lastrowid)
            for ref in refs:
                item = by_ref[ref]
                self.conn.execute(
                    """INSERT INTO report_entries(batch_id,item_id,external_ref,
                       item_version,result) VALUES(?,?,?,?,?)""",
                    (batch_id, item["id"], ref, item["version"], ENTRY_PENDING),
                )
            self._append_audit_locked("report_batch_create", "report_batch", batch_id,
                                      actor, {"batch_no": batch_no,
                                              "external_refs": refs})
            batch = self._batch(self.conn.execute(
                "SELECT * FROM report_batches WHERE id=?", (batch_id,)).fetchone())
            entry_rows = self.conn.execute(
                "SELECT * FROM report_entries WHERE batch_id=? ORDER BY id",
                (batch_id,)).fetchall()
        return {"batch": batch, "entries": [self._entry(r) for r in entry_rows]}

    def _next_batch_no_locked(self) -> str:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM report_batches").fetchone()
        return f"{BATCH_PREFIX}-{int(row['n']) + 1:08d}"

    def get_batch(self, batch_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM report_batches WHERE batch_no=?", (batch_no,)).fetchone()
            if row is None:
                raise NotFoundError("批次不存在")
            entries = self.conn.execute(
                "SELECT * FROM report_entries WHERE batch_id=? ORDER BY id",
                (row["id"],)).fetchall()
        return {"batch": self._batch(row),
                "entries": [self._entry(r) for r in entries]}

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM report_batches ORDER BY id DESC").fetchall()
        return [self._batch(r) for r in rows]

    def dispatch_batch(self, batch_no: str, allowed_status: List[str],
                       ledger: Any, actor: str) -> Dict[str, Any]:
        """确认/重试批次：对台账提交、按外部编号对账、回写项目与审计，单事务提交。

        台账传输失败时同样在单事务内把批次置为 failed 并留审计后提交，
        项目状态不产生任何变更（不存在改了项目没写审计的半成品）。
        """
        now = utc_now()
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                brow = self.conn.execute(
                    "SELECT * FROM report_batches WHERE batch_no=?", (batch_no,)).fetchone()
                if brow is None:
                    raise NotFoundError("批次不存在")
                batch = self._batch(brow)
                if batch["status"] not in allowed_status:
                    raise ConflictError(
                        f"批次当前状态为{batch['status']}，不能执行该操作")
                entry_rows = self.conn.execute(
                    "SELECT * FROM report_entries WHERE batch_id=? ORDER BY id",
                    (batch["id"],)).fetchall()
                item_ids = [int(r["item_id"]) for r in entry_rows]
                open_counts = self.open_record_counts(item_ids)
                item_rows = self.conn.execute(
                    "SELECT * FROM items WHERE id IN ({})".format(
                        ",".join("?" for _ in item_ids)), tuple(item_ids)).fetchall()
                items = {int(r["id"]): self._item(r) for r in item_rows}
                payload = []
                conclusions = {}
                for er in entry_rows:
                    item = items[int(er["item_id"])]
                    conclusion = report_conclusion(item, open_counts.get(item["id"], 0))
                    conclusions[er["external_ref"]] = conclusion
                    payload.append({"external_ref": er["external_ref"],
                                    "version": item["version"], "conclusion": conclusion})
                action = ("report_batch_retry" if batch["status"] == BATCH_FAILED
                          else "report_batch_confirm")
                try:
                    receipts = ledger.submit(batch_no, payload)
                except UpstreamError as exc:
                    # 失败也要留痕：批次置 failed + 审计事件，随事务一起提交
                    self.conn.execute(
                        """UPDATE report_batches SET status=?, fail_reason=?,
                           confirmed_by=?, updated_at=? WHERE id=?""",
                        (BATCH_FAILED, str(exc), actor, now, batch["id"]))
                    self._append_audit_locked("report_batch_failed", "report_batch",
                                              batch["id"], actor,
                                              {"batch_no": batch_no, "reason": str(exc)})
                    self.conn.commit()
                    raise
                results = ledger.reconcile(payload, receipts)
                result_by_ref = {r["external_ref"]: r for r in results}
                summary = {"reported": 0, "mismatch": 0, "missing": 0}
                for er in entry_rows:
                    ref = er["external_ref"]
                    outcome = result_by_ref.get(ref, {"result": "missing",
                                                      "receipt_version": None, "diffs": []})
                    result = outcome["result"]
                    summary[result] = summary.get(result, 0) + 1
                    local_conclusion = conclusions[ref]
                    self.conn.execute(
                        """UPDATE report_entries SET result=?, receipt_version=?, diffs=?,
                           reported_conclusion=? WHERE id=?""",
                        (result, outcome.get("receipt_version"),
                         json.dumps(outcome.get("diffs", []), ensure_ascii=False),
                         json.dumps(local_conclusion, ensure_ascii=False, sort_keys=True),
                         er["id"]))
                    if result in ("reported", "mismatch"):
                        # 回执版本不同也保留本地值：记录本地版本与本地结论，差异另列
                        item = items[int(er["item_id"])]
                        self.conn.execute(
                            """UPDATE items SET reported_version=?, reported_conclusion=?,
                               reported_batch_no=?, reported_at=?, report_stale=0, updated_at=?
                               WHERE id=?""",
                            (item["version"],
                             json.dumps(local_conclusion, ensure_ascii=False, sort_keys=True),
                             batch_no, now, now, item["id"]))
                        self._append_audit_locked(
                            "report_item_" + result, ENTITY, item["id"], actor, {
                                "batch_no": batch_no, "external_ref": ref,
                                "local_version": item["version"],
                                "receipt_version": outcome.get("receipt_version"),
                                "diffs": outcome.get("diffs", []),
                            })
                self.conn.execute(
                    """UPDATE report_batches SET status=?, fail_reason=NULL,
                       confirmed_by=?, updated_at=? WHERE id=?""",
                    (BATCH_SUBMITTED, actor, now, batch["id"]))
                self._append_audit_locked(action, "report_batch", batch["id"], actor, {
                    "batch_no": batch_no, "summary": summary,
                })
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_batch(batch_no)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            return self._append_audit_locked(action, entity_type, entity_id,
                                             actor, detail)

    def _append_audit_locked(self, action: str, entity_type: str, entity_id: int,
                             actor: str, detail: dict) -> Dict[str, Any]:
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
        event["id"] = int(cur.lastrowid)
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

    def close(self) -> None:
        with self._lock:
            self.conn.close()
