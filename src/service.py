from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ReportFailed, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .ledger import LedgerError, RegulatoryLedger
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_CREATE_ROLES, CONFIRM_ROLES, CREATE_ROLES,
                    RECORD_ROLES, VIEW_ROLES, completion_blockers, conclusion_diff,
                    escalation_required, priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository,
                 ledger: Optional[RegulatoryLedger] = None):
        self.repository = repository
        self.ledger = ledger or RegulatoryLedger()

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    # ---------- 鉴定项目 ----------

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        audit_detail = {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        }
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor, audit_detail)
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        audit_detail = {"kind": kind, "status": status}
        record, invalidated = self.repository.add_record(
            item_id, kind, detail, status, external_ref, actor, audit_detail)
        record["invalidated_batches"] = invalidated
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        audit_detail = {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        }
        updated = self.repository.transition_item(
            item_id, target, expected_version, actor, audit_detail)
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---------- 上报批次 ----------

    def create_batch(self, item_ids: List[int], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        if not isinstance(item_ids, list) or not item_ids:
            raise ValidationError("item_ids必须是非空数组")
        snapshots = []
        for item_id in item_ids:
            item = self.repository.get_item(int(item_id))
            if not item["external_ref"]:
                raise ValidationError(f"项目{item_id}缺少外部编号，无法按外部编号对账")
            snapshots.append((item["id"], item["external_ref"],
                              self._conclusion_snapshot(item), item["version"]))
        batch_no = f"BN-{uuid.uuid4().hex[:12].upper()}"
        return self.repository.create_batch(batch_no, actor, snapshots)

    def confirm_batch(self, batch_id: int, actor: str, role: str) -> Dict[str, Any]:
        return self._submit_batch(batch_id, actor, role, ("pending",))

    def retry_batch(self, batch_id: int, actor: str, role: str) -> Dict[str, Any]:
        return self._submit_batch(batch_id, actor, role, ("failed",))

    def _submit_batch(self, batch_id: int, actor: str, role: str,
                      allowed_from: tuple) -> Dict[str, Any]:
        ensure_role(role, CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.repository.get_batch(batch_id)
        if batch["status"] == "confirmed":
            raise ConflictError("批次已确认，请勿重复上报")
        if batch["status"] not in allowed_from:
            raise ConflictError(f"批次状态为{batch['status']}，不允许该操作")
        ledger_items = [(it["external_ref"], it["local_version"], it["snapshot"])
                        for it in batch["items"]]
        try:
            receipt = self.ledger.submit_batch(batch["batch_no"], ledger_items)
        except LedgerError as exc:
            # 失败也落库 failed + 审计，可按同一批次号重试
            self.repository.mark_batch_failed(batch_id, str(exc), actor)
            raise ReportFailed(
                f"批次上报失败：{exc}，可按同一批次号重试", batch_id) from exc
        item_results = []
        batch_diff = []
        for it, rcpt in zip(batch["items"], receipt.items):
            if rcpt.conflict:
                # 回执版本不同：保留本地值，逐字段列出差异
                diff = conclusion_diff(it["snapshot"], rcpt.conclusion)
                item_results.append((it["item_id"], rcpt.receipt_version, "diff", diff))
                batch_diff.extend(diff)
            else:
                item_results.append((it["item_id"], rcpt.receipt_version, "matched", []))
        return self.repository.mark_batch_confirmed(
            batch_id, actor, receipt.receipt_no, batch_diff, item_results)

    def list_batches(self, role: str) -> List[Dict[str, Any]]:
        self._view(role)
        return self.repository.list_batches()

    def get_batch(self, batch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_batch(batch_id)

    def _conclusion_snapshot(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": item["status"],
            "severity": item["severity"],
            "quantity": item["quantity"],
            "threshold": item["threshold"],
            "priority": priority_score(item["severity"], item["quantity"],
                                       item["threshold"]),
            "records_open": self.repository.open_record_count(item["id"]),
            "records_total": len(self.repository.list_records(item["id"])),
        }

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
