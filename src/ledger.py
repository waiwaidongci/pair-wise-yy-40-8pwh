from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from .domain import UpstreamError
from .rules import conclusion_diff


class RegulatoryLedger:
    """监管台账的适配器边界。

    生产环境替换 submit 的传输实现即可；接口约定：
    - 同一 batch_no 重复提交为幂等操作，返回首次提交时的回执；
    - 回执按 external_ref 携带台账当前保存的版本与结论；
    - 传输/对端失败以 UpstreamError 抛出，本地不得产生半成品。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._batches: Dict[str, List[Dict[str, Any]]] = {}
        self._fail_once: Dict[str, int] = {}
        self._seeded: Dict[str, Dict[str, Any]] = {}

    # --- 测试/装配用钩子 -------------------------------------------------
    def fail_next_submit(self, times: int = 1) -> None:
        with self._lock:
            self._fail_once["*"] = self._fail_once.get("*", 0) + times

    def seed_ledger_item(self, external_ref: str, version: int,
                         conclusion: Dict[str, Any]) -> None:
        """模拟台账里已有他人/早先提交的数据（用于对账差异场景）。"""
        with self._lock:
            self._seeded[external_ref] = {
                "external_ref": external_ref, "version": version,
                "conclusion": dict(conclusion),
            }

    def stored(self, external_ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            saved = self._find_saved(external_ref)
            return dict(saved) if saved else None

    # --- 适配器接口 -------------------------------------------------------
    def submit(self, batch_no: str, entries: List[Dict[str, Any]]
               ) -> List[Dict[str, Any]]:
        """把批次提交给监管台账，返回每个 external_ref 的回执。

        entries: [{'external_ref','version','conclusion'}, ...]
        回执: [{'external_ref','version','conclusion'}, ...]（台账视角当前值）

        台账按外部编号以“最后写入”保存；若本地基线版本晚于/异于台账现存版本，
        台账拒绝覆盖并回传台账现存值，由本地对账发现差异并保留本地值。
        """
        with self._lock:
            if self._fail_once.get("*", 0) > 0:
                self._fail_once["*"] -= 1
                raise UpstreamError("监管台账暂不可用")
            existing = self._batches.get(batch_no)
            if existing is not None:
                # 幂等：同一批次号重试，返回台账当前视角
                return [self._view_for(item["external_ref"]) for item in existing]
            stored: List[Dict[str, Any]] = []
            for entry in entries:
                ref = entry["external_ref"]
                current = self._seeded.get(ref)
                if current is None:
                    item = {"external_ref": ref, "version": entry["version"],
                            "conclusion": dict(entry["conclusion"])}
                    self._seeded[ref] = item
                elif entry["version"] >= current["version"]:
                    # 同版本或更新版本：接受本次写入
                    current["version"] = entry["version"]
                    current["conclusion"] = dict(entry["conclusion"])
                    item = {"external_ref": ref, "version": current["version"],
                            "conclusion": dict(current["conclusion"])}
                else:
                    # 基线版本比台账现存版本旧（他人已写入）：拒绝覆盖，回传台账现存值
                    item = {"external_ref": ref, "version": current["version"],
                            "conclusion": dict(current["conclusion"])}
                stored.append(item)
            self._batches[batch_no] = stored
            return [self._view_for(item["external_ref"]) for item in stored]

    def reconcile(self, local_entries: List[Dict[str, Any]],
                  receipts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按外部编号对账：版本不同则保留本地值，输出字段级差异。"""
        by_ref = {r["external_ref"]: r for r in receipts}
        results = []
        for entry in local_entries:
            ref = entry["external_ref"]
            receipt = by_ref.get(ref)
            if receipt is None:
                results.append({"external_ref": ref, "result": "missing",
                                "receipt_version": None, "diffs": []})
                continue
            diffs = conclusion_diff(entry["conclusion"], receipt.get("conclusion"))
            if receipt.get("version") != entry["version"] or diffs:
                results.append({"external_ref": ref, "result": "mismatch",
                                "receipt_version": receipt.get("version"),
                                "local_version": entry["version"], "diffs": diffs})
            else:
                results.append({"external_ref": ref, "result": "reported",
                                "receipt_version": receipt.get("version"), "diffs": []})
        return results

    def _find_saved(self, external_ref: str) -> Optional[Dict[str, Any]]:
        item = self._seeded.get(external_ref)
        return dict(item) if item else None

    def _view_for(self, external_ref: str) -> Dict[str, Any]:
        item = self._seeded[external_ref]
        return {"external_ref": external_ref, "version": item["version"],
                "conclusion": dict(item["conclusion"])}
