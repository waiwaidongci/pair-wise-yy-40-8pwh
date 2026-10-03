from __future__ import annotations

from typing import Any, Dict, List, Tuple


class LedgerError(Exception):
    """上报监管台账失败（可重试）。"""


class Receipt:
    """单条鉴定结论的台账回执。"""

    __slots__ = ("external_ref", "receipt_version", "conclusion", "conflict")

    def __init__(self, external_ref: str, receipt_version: int,
                 conclusion: Dict[str, Any], conflict: bool):
        self.external_ref = external_ref
        self.receipt_version = receipt_version
        self.conclusion = conclusion
        self.conflict = conflict


class BatchReceipt:
    """整批上报的台账回执。"""

    __slots__ = ("receipt_no", "items")

    def __init__(self, receipt_no: str, items: List[Receipt]):
        self.receipt_no = receipt_no
        self.items = items


class RegulatoryLedger:
    """监管台账客户端（内存实现）。

    按外部编号（external_ref）存储结论与版本。上报时：
    - 台账已有版本高于本地版本（人工/并发上报改过）→ 冲突，不覆盖，
      回执带回台账当前结论，由调用方保留本地值并列出差异；
    - 否则接受本地版本并落账。
    支持注入一次上报失败，用于验证"同一批次号重试"。
    """

    def __init__(self):
        self._store: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self._fail_next = False

    def inject_failure(self) -> None:
        """让下一次整批上报失败一次。"""
        self._fail_next = True

    def manual_update(self, external_ref: str, conclusion: Dict[str, Any],
                      version: Optional[int] = None) -> int:
        """模拟台账的人工/并发上报：直接抬高台账版本（默认在原版本上 +1）。"""
        current, _ = self._store.get(external_ref, (0, None))
        if version is None:
            version = current + 1
        self._store[external_ref] = (version, dict(conclusion))
        return version

    def submit_batch(self, batch_no: str,
                     items: List[Tuple[str, int, Dict[str, Any]]]) -> BatchReceipt:
        if self._fail_next:
            self._fail_next = False
            raise LedgerError("监管台账上报失败")
        receipts = [self._submit_one(ref, version, conclusion)
                    for ref, version, conclusion in items]
        return BatchReceipt(f"RCPT-{batch_no}", receipts)

    def _submit_one(self, external_ref: str, local_version: int,
                    conclusion: Dict[str, Any]) -> Receipt:
        current_version, current = self._store.get(external_ref, (0, None))
        if current_version > local_version:
            # 台账已被更新的版本覆盖：保留本地值，标记冲突
            return Receipt(external_ref, current_version, dict(current), True)
        self._store[external_ref] = (local_version, dict(conclusion))
        return Receipt(external_ref, local_version, dict(conclusion), False)
