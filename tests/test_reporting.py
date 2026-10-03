import tempfile, threading, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, UpstreamError
from src.ledger import RegulatoryLedger
from src.repository import Repository
from src.service import Service


class ReportBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = RegulatoryLedger()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo, self.ledger)
        self.item = self.service.create_item(
            {"title": "report item", "description": "to ledger",
             "severity": "high", "quantity": 5, "threshold": 10,
             "external_ref": "REG-1"}, "creator", "assessor")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _batch(self, refs=("REG-1",), actor="creator", role="assessor"):
        return self.service.create_report_batch(
            {"external_refs": list(refs)}, actor, role)

    def test_assessor_cannot_confirm_batch(self):
        self._batch()
        with self.assertRaises(PermissionDenied):
            self.service.confirm_batch({"batch_no": "RB-00000001"},
                                       "creator", "assessor")
        # 被拒绝后批次仍是草稿
        self.assertEqual(self.service.get_batch("RB-00000001", "viewer")
                         ["batch"]["status"], "draft")

    def test_board_confirm_reports_and_audits_atomically(self):
        self._batch()
        result = self.service.confirm_batch({"batch_no": "RB-00000001"},
                                            "board", "review_board")
        self.assertEqual(result["batch"]["status"], "submitted")
        self.assertEqual(result["entries"][0]["result"], "reported")
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["reported_version"], item["version"])
        self.assertEqual(item["reported_batch_no"], "RB-00000001")
        self.assertFalse(item["report_stale"])
        events = self.service.audit("viewer")
        actions = [e["action"] for e in events]
        self.assertIn("report_batch_confirm", actions)
        self.assertIn("report_item_reported", actions)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_reconcile_by_external_ref_keeps_local_and_lists_diff(self):
        # 台账里已存在另一人提交的旧版本，结论也不同
        self.ledger.seed_ledger_item("REG-1", 999, {
            "status": "assessed", "severity": "low", "priority": 1,
            "quantity": 1.0, "threshold": 1.0, "deadline_hours": 72,
            "escalation_required": False})
        self._batch()
        result = self.service.confirm_batch({"batch_no": "RB-00000001"},
                                            "board", "review_board")
        entry = result["entries"][0]
        self.assertEqual(entry["result"], "mismatch")
        self.assertEqual(entry["receipt_version"], 999)
        fields = {d["field"] for d in entry["diffs"]}
        self.assertIn("status", fields)
        self.assertIn("severity", fields)
        # 本地值不被回执覆盖
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["reported_conclusion"]["severity"], "high")
        self.assertEqual(item["reported_version"], item["version"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_evidence_update_invalidates_report_and_recalculates(self):
        self._batch()
        self.service.confirm_batch({"batch_no": "RB-00000001"},
                                   "board", "review_board")
        before = self.service.get_item(self.item["id"], "viewer")
        self.service.add_record(
            self.item["id"],
            {"kind": "evidence", "detail": "new crack evidence",
             "status": "closed", "external_ref": "EV-NEW"},
            "recorder", "assessor")
        after = self.service.get_item(self.item["id"], "viewer")
        self.assertTrue(after["report_stale"])
        self.assertEqual(after["version"], before["version"] + 1)
        actions = [e["action"] for e in self.service.audit("viewer")]
        self.assertIn("report_invalidated", actions)
        self.assertTrue(self.repo.verify_audit_chain())
        # 失效后可重新成批上报，结论按新数据重算
        self._batch()
        again = self.service.confirm_batch({"batch_no": "RB-00000002"},
                                           "board", "review_board")
        self.assertEqual(again["entries"][0]["result"], "reported")
        refreshed = self.service.get_item(self.item["id"], "viewer")
        self.assertFalse(refreshed["report_stale"])

    def test_failed_submit_retries_with_same_batch_number(self):
        self._batch()
        self.ledger.fail_next_submit(1)
        with self.assertRaises(UpstreamError):
            self.service.confirm_batch({"batch_no": "RB-00000001"},
                                       "board", "review_board")
        failed = self.service.get_batch("RB-00000001", "viewer")
        self.assertEqual(failed["batch"]["status"], "failed")
        self.assertTrue(failed["batch"]["fail_reason"])
        self.assertEqual(failed["entries"][0]["result"], "pending")
        # 失败未改动项目上报状态，且失败本身有审计
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertIsNone(item["reported_version"])
        actions = [e["action"] for e in self.service.audit("viewer")]
        self.assertIn("report_batch_failed", actions)
        self.assertTrue(self.repo.verify_audit_chain())
        # 必须用同一批次号重试；对草稿再次确认会冲突
        with self.assertRaises(ConflictError):
            self.service.confirm_batch({"batch_no": "RB-00000001"},
                                       "board", "review_board")
        result = self.service.retry_batch({"batch_no": "RB-00000001"},
                                          "board", "review_board")
        self.assertEqual(result["batch"]["status"], "submitted")
        self.assertEqual(result["entries"][0]["result"], "reported")
        # 台账按批次号幂等：再次重试已提交批次应冲突
        with self.assertRaises(ConflictError):
            self.service.retry_batch({"batch_no": "RB-00000001"},
                                     "board", "review_board")

    def test_same_external_ref_locked_in_two_active_batches(self):
        other = self.service.create_item(
            {"title": "other", "description": "x", "severity": "low",
             "quantity": 1, "threshold": 1, "external_ref": "REG-2"},
            "creator", "assessor")
        del other
        self._batch(["REG-1", "REG-2"])
        # 草稿未确认，同一编号不能再进新批次（两人并发提交同一项目）
        with self.assertRaises(ConflictError):
            self._batch(["REG-1"])
        with self.assertRaises(ConflictError):
            self._batch(["REG-2"])
        self.service.confirm_batch({"batch_no": "RB-00000001"},
                                   "board", "review_board")
        # 已提交后解锁，可用于新批次（重报场景）
        second = self._batch(["REG-1"])
        self.assertEqual(second["batch"]["batch_no"], "RB-00000002")

    def test_concurrent_confirm_only_one_dispatch_wins(self):
        self._batch()
        outcomes = []

        def confirm():
            try:
                outcomes.append(self.service.confirm_batch(
                    {"batch_no": "RB-00000001"}, "board", "review_board"))
            except ConflictError as exc:
                outcomes.append(exc)

        threads = [threading.Thread(target=confirm) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(outcomes), 2)
        success = [o for o in outcomes if not isinstance(o, Exception)]
        conflicts = [o for o in outcomes if isinstance(o, ConflictError)]
        self.assertEqual(len(success), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
