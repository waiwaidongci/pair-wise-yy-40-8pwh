import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ReportFailed, ValidationError
from src.ledger import RegulatoryLedger
from src.repository import Repository
from src.service import Service


class ReportBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.ledger = RegulatoryLedger()
        self.service = Service(self.repo, self.ledger)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _item(self, ref, severity="high", quantity=5, threshold=10):
        return self.service.create_item(
            {"title": f"item {ref}", "description": "desc", "severity": severity,
             "quantity": quantity, "threshold": threshold, "external_ref": ref},
            "creator", "assessor")

    def _events(self, action):
        return [e for e in self.service.audit("review_board") if e["action"] == action]

    def test_confirm_batch_matched(self):
        i1 = self._item("M-1")
        i2 = self._item("M-2", severity="medium")
        batch = self.service.create_batch([i1["id"], i2["id"]], "clerk", "assessor")
        self.assertEqual(batch["status"], "pending")

        confirmed = self.service.confirm_batch(batch["id"], "reviewer", "review_board")
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertTrue(confirmed["receipt_no"].startswith("RCPT-"))
        for it in confirmed["items"]:
            self.assertEqual(it["receipt_status"], "matched")
            self.assertEqual(it["receipt_version"], it["local_version"])
            self.assertEqual(it["diff"], [])
        # 批次创建与确认都进了审计链
        self.assertEqual(len(self._events("batch_created")), 1)
        self.assertEqual(len(self._events("batch_confirmed")), 1)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_confirm_diff_keeps_local_and_lists_diff(self):
        item = self._item("D-1")
        batch = self.service.create_batch([item["id"]], "clerk", "assessor")
        local_version = batch["items"][0]["local_version"]
        # 模拟人工/并发上报把台账版本抬到本地之上，且结论被改成 accepted/severe
        self.ledger.manual_update("D-1", {
            "status": "accepted", "severity": "severe", "quantity": 99,
            "threshold": 1, "priority": 10, "records_open": 0, "records_total": 0,
        }, version=local_version + 1)

        confirmed = self.service.confirm_batch(batch["id"], "reviewer", "review_board")
        it = confirmed["items"][0]
        self.assertEqual(it["receipt_status"], "diff")
        self.assertEqual(it["receipt_version"], local_version + 1)
        # 本地值保留，未被台账回执覆盖
        self.assertEqual(it["snapshot"]["status"], "proposed")
        self.assertEqual(it["snapshot"]["severity"], "high")
        # 差异逐字段列出
        fields = {d["field"]: d for d in it["diff"]}
        self.assertEqual(fields["status"]["local"], "proposed")
        self.assertEqual(fields["status"]["receipt"], "accepted")
        self.assertEqual(fields["severity"]["local"], "high")
        self.assertEqual(fields["severity"]["receipt"], "severe")
        self.assertTrue(confirmed["diff"])

    def test_assessor_confirm_denied(self):
        item = self._item("P-1")
        batch = self.service.create_batch([item["id"]], "clerk", "assessor")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_batch(batch["id"], "assessor", "assessor")
        with self.assertRaises(PermissionDenied):
            self.service.retry_batch(batch["id"], "assessor", "assessor")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_batch(batch["id"], "viewer", "viewer")

    def test_evidence_update_invalidates_confirmed_conclusion(self):
        item = self._item("E-1")
        batch = self.service.create_batch([item["id"]], "clerk", "assessor")
        self.service.confirm_batch(batch["id"], "reviewer", "review_board")

        record = self.service.add_record(
            item["id"], {"kind": "evidence", "detail": "new evidence",
                         "status": "closed", "external_ref": "EV-1"},
            "recorder", "assessor")
        self.assertEqual(record["invalidated_batches"], [batch["id"]])

        stale = self.service.get_batch(batch["id"], "review_board")
        self.assertTrue(stale["items"][0]["stale"])
        # 原上报结论快照仍保留，未被改写
        self.assertEqual(stale["items"][0]["snapshot"]["status"], "proposed")
        rec_events = self._events("record")
        self.assertEqual(rec_events[-1]["detail"]["invalidated_batches"], [batch["id"]])

        # 重算后重新建批次上报
        new_batch = self.service.create_batch([item["id"]], "clerk", "assessor")
        self.assertNotEqual(new_batch["batch_no"], batch["batch_no"])
        confirmed = self.service.confirm_batch(new_batch["id"], "reviewer", "review_board")
        self.assertEqual(confirmed["status"], "confirmed")
        # 旧批次条目的失效标记依旧
        self.assertTrue(self.service.get_batch(batch["id"], "review_board")
                        ["items"][0]["stale"])

    def test_failed_report_retry_same_batch_no(self):
        item = self._item("F-1")
        batch = self.service.create_batch([item["id"]], "clerk", "assessor")
        self.ledger.inject_failure()

        with self.assertRaises(ReportFailed) as ctx:
            self.service.confirm_batch(batch["id"], "reviewer", "review_board")
        self.assertEqual(ctx.exception.batch_id, batch["id"])

        failed = self.service.get_batch(batch["id"], "review_board")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("监管台账上报失败", failed["last_error"])
        self.assertEqual(len(self._events("batch_failed")), 1)
        original_no = failed["batch_no"]

        retried = self.service.retry_batch(batch["id"], "reviewer", "review_board")
        self.assertEqual(retried["status"], "confirmed")
        self.assertEqual(retried["batch_no"], original_no)
        self.assertTrue(retried["receipt_no"].startswith("RCPT-"))
        self.assertEqual(len(self._events("batch_confirmed")), 1)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_confirm_twice_conflict(self):
        item = self._item("T-1")
        batch = self.service.create_batch([item["id"]], "clerk", "assessor")
        self.service.confirm_batch(batch["id"], "reviewer", "review_board")
        with self.assertRaises(ConflictError):
            self.service.confirm_batch(batch["id"], "reviewer", "review_board")

    def test_retry_pending_conflict(self):
        item = self._item("R-1")
        batch = self.service.create_batch([item["id"]], "clerk", "assessor")
        with self.assertRaises(ConflictError):
            self.service.retry_batch(batch["id"], "reviewer", "review_board")

    def test_create_requires_external_ref_and_role(self):
        item = self.service.create_item(
            {"title": "no ref", "description": "d", "severity": "low"},
            "creator", "assessor")
        with self.assertRaises(ValidationError):
            self.service.create_batch([item["id"]], "clerk", "assessor")
        with self.assertRaises(PermissionDenied):
            self.service.create_batch([item["id"]], "clerk", "viewer")

    def test_batch_status_and_audit_written_together(self):
        item = self._item("A-1")
        batch = self.service.create_batch([item["id"]], "clerk", "assessor")
        # 待确认批次也必须有 batch_created 审计，不存在"改了状态没写审计"的半成品
        self.assertEqual(len(self._events("batch_created")), 1)
        self.assertEqual(self._events("batch_created")[0]["entity_id"], batch["id"])
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
