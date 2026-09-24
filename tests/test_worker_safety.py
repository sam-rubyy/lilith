import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from lilith.database import Database
from lilith.redaction import redact_text
from lilith.tasks import TaskStore
from lilith.worker_logs import WorkerLog, LIMIT, LINE_LIMIT
from lilith.workers import WorkerManager


class WorkerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "state.db")
        self.addCleanup(self.db.close)
        self.store = TaskStore(self.db)

    def test_worker_log_caps_output_and_preserves_exit_summary(self):
        log = WorkerLog(self.root / "worker.log", io.BytesIO((b"diagnostic\n" * 150000)))
        log.finish("\n[supervisor] exit_code=3\n")
        self.assertLessEqual(log.path.stat().st_size, LIMIT)
        self.assertIn("exit_code=3", log.path.read_text())
        self.assertFalse(log.thread.is_alive())

    def test_log_redaction_handles_chunk_boundaries_and_oversized_lines(self):
        secret = "disposable-diagnostic-token"
        body = (b"x" * 8180 + secret.encode() + b"\n" + b"x" * (LINE_LIMIT + 1) + secret.encode() + b"\n")
        with patch.dict(os.environ, {"LILITH_TEST_TOKEN": secret}):
            log = WorkerLog(self.root / "worker.log", io.BytesIO(body))
            log.finish("finished\n")
        text = log.path.read_text()
        self.assertNotIn(secret, text)
        self.assertIn("[redacted]", text)
        self.assertIn("oversized worker line omitted", text)

    def test_task_errors_and_audits_redact_known_secrets_and_url_credentials(self):
        ident = self.store.enqueue("journal", {})
        self.store.claim("worker", ["journal"])
        secret = 'temporary-"token"-value'
        with patch.dict(os.environ, {"LILITH_TEST_TOKEN": secret}):
            self.store.interrupted(ident, f"failed {secret} https://owner:password@example.com/path?token=hidden")
            self.db.audit("test", json.dumps({"error": secret, "url": "https://owner:password@example.com/?token=hidden"}))
        retained = json.dumps(self.store.rows("SELECT message FROM audit_events")) + self.store.get(ident)["error"]
        self.assertNotIn("temporary", retained)
        self.assertNotIn("password", retained)
        self.assertNotIn("hidden", retained)
        # Audit events remain valid JSON after redaction.
        self.assertEqual(json.loads(self.store.rows("SELECT message FROM audit_events WHERE event_type='test'")[0]["message"])["error"], "[redacted]")

    def test_launch_gate_eof_prevents_execution(self):
        ident = self.store.enqueue("journal", {"body": "must not be written"})
        self.store.claim("worker", ["journal"])
        proc = subprocess.run([sys.executable, "-m", "lilith.workers", str(self.db.path),
                               str(ident), str(self.root), "unused", "--managed"],
                              input=b"", capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.store.rows("SELECT id FROM journal_entries"))

    def test_recovery_does_not_kill_reused_pid(self):
        with self.store.transaction() as c:
            c.execute("INSERT INTO workers(id,heartbeat,state,pid,process_created) VALUES ('old','now','running',98765,10)")
        process = MagicMock()
        process.create_time.return_value = 11
        with patch("psutil.Process", return_value=process):
            WorkerManager(self.db, self.root, "unused")._recover_processes()
        process.kill.assert_not_called()
        self.assertTrue(self.store.rows("SELECT id FROM audit_events WHERE event_type='worker_identity_mismatch'"))

    def test_recovery_stops_matching_orphan_before_task_recovery(self):
        import psutil
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            ident = self.store.enqueue("journal", {"body": "interrupted"})
            self.store.claim("orphan", ["journal"])
            with self.store.transaction() as c:
                c.execute("UPDATE workers SET pid=?,process_created=? WHERE id='orphan'",
                          (proc.pid, psutil.Process(proc.pid).create_time()))
            manager = WorkerManager(self.db, self.root, "unused")
            manager.start()
            try:
                self.assertIsNotNone(proc.poll())
                self.assertEqual(self.store.get(ident)["state"], "needs_review")
            finally:
                manager.close()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
