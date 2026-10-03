from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ValidationError, ensure_role, normalize_severity,
                     require_number, require_text)
from .ledger import RegulatoryLedger
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_CONFIRM_ROLES, BATCH_CREATE_ROLES,
                    BATCH_DRAFT, BATCH_FAILED, BATCH_VIEW_ROLES,
                    CREATE_ROLES, ENTITY, MAX_BATCH_ITEMS, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository,
                 ledger: Optional[RegulatoryLedger] = None):
        self.repository = repository
        self.ledger = ledger or RegulatoryLedger()

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

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
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
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
        # 证据登记、版本递增、上报失效与审计在仓储层同一事务提交
        return self.repository.add_record(item_id, kind, detail, status,
                                          external_ref, actor)

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
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
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

    # --- 监管台账批次上报 -------------------------------------------------
    def _batch_refs(self, payload: Dict[str, Any]) -> list:
        refs = payload.get("external_refs")
        if not isinstance(refs, list) or not refs:
            raise ValidationError("external_refs必须是非空数组")
        if len(refs) > MAX_BATCH_ITEMS:
            raise ValidationError(f"单批次最多{MAX_BATCH_ITEMS}个项目")
        cleaned = []
        for ref in refs:
            cleaned.append(require_text(ref, "external_ref", 100))
        if len(set(cleaned)) != len(cleaned):
            raise ValidationError("批次内external_ref不能重复")
        return cleaned

    def create_report_batch(self, payload: Dict[str, Any], actor: str,
                            role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        refs = self._batch_refs(payload)
        return self.repository.create_report_batch(refs, actor)

    def confirm_batch(self, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        # 只有复核委员会能确认上报；鉴定员越权确认直接拒绝
        ensure_role(role, BATCH_CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        return self.repository.dispatch_batch(
            batch_no, [BATCH_DRAFT], self.ledger, actor)

    def retry_batch(self, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        # 失败后按同一批次号重试，禁止新建批次号
        ensure_role(role, BATCH_CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        return self.repository.dispatch_batch(
            batch_no, [BATCH_FAILED], self.ledger, actor)

    def get_batch(self, batch_no: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_VIEW_ROLES)
        return self.repository.get_batch(batch_no)

    def list_batches(self, role: str) -> list:
        ensure_role(role, BATCH_VIEW_ROLES)
        return self.repository.list_batches()

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
