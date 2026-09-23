import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from lilith.database import Database, utc_now
from lilith.models import ModelArbiter, ModelRole, gateway
from lilith.tasks import TaskStore


class ModelArbitrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "state.db")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_conversation_overtakes_waiting_background_request(self):
        arbiter = ModelArbiter(self.db, lease_seconds=5, poll_interval=0.005)
        blocker = arbiter.acquire(ModelRole.MAINTENANCE)
        acquired = []

        def wait_for(role):
            request = arbiter.acquire(role)
            acquired.append(role)
            arbiter.release(request)

        background = threading.Thread(target=wait_for, args=(ModelRole.BACKGROUND,))
        conversation = threading.Thread(target=wait_for, args=(ModelRole.CONVERSATION,))
        background.start()
        deadline = time.monotonic() + 2
        while self.db.connection.execute(
            "SELECT COUNT(*) FROM model_requests WHERE state='waiting'"
        ).fetchone()[0] < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        conversation.start()
        deadline = time.monotonic() + 2
        while self.db.connection.execute(
            "SELECT COUNT(*) FROM model_requests WHERE state='waiting'"
        ).fetchone()[0] < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        arbiter.release(blocker)
        conversation.join(2)
        background.join(2)
        self.assertEqual(acquired, [ModelRole.CONVERSATION, ModelRole.BACKGROUND])

    def test_stale_lease_is_recovered_and_audited(self):
        arbiter = ModelArbiter(self.db, poll_interval=0.005)
        stale_id = "stale"
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.db.lock:
            self.db.connection.execute(
                "INSERT INTO model_requests(id,role,priority,created_at,state) VALUES (?,?,?,?,?)",
                (stale_id, "background", 30, utc_now(), "active"),
            )
            self.db.connection.execute(
                "INSERT INTO model_lease VALUES (1,?,?,?,?,?)",
                (stale_id, "background", past, past, past),
            )
            self.db.connection.commit()
        current = arbiter.acquire(ModelRole.CONVERSATION)
        arbiter.release(current)
        state = self.db.connection.execute(
            "SELECT state FROM model_requests WHERE id=?", (stale_id,)
        ).fetchone()[0]
        self.assertEqual(state, "expired")
        self.assertIsNotNone(self.db.connection.execute(
            "SELECT 1 FROM audit_events WHERE event_type='model_lease_expired'"
        ).fetchone())

    def test_cancellation_removes_waiting_request(self):
        arbiter = ModelArbiter(self.db, lease_seconds=5, poll_interval=0.005)
        blocker = arbiter.acquire(ModelRole.CONVERSATION)
        cancelled = threading.Event()
        errors = []

        def check():
            if cancelled.is_set():
                raise RuntimeError("cancelled")

        def wait():
            try:
                arbiter.acquire(ModelRole.BACKGROUND, check=check)
            except RuntimeError as error:
                errors.append(str(error))

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.03)
        cancelled.set()
        thread.join(2)
        arbiter.release(blocker)
        self.assertEqual(errors, ["cancelled"])
        self.assertEqual(self.db.connection.execute(
            "SELECT state FROM model_requests WHERE role='background'"
        ).fetchone()[0], "cancelled")
        self.assertIsNone(self.db.connection.execute("SELECT * FROM model_lease").fetchone())

    def test_role_models_default_to_base_and_allow_override(self):
        with patch.dict(os.environ, {"LILITH_MODEL": "base", "LILITH_REFLECTION_MODEL": "reflect"}, clear=False):
            self.assertEqual(gateway(self.db, ModelRole.CONVERSATION).model_name, "base")
            self.assertEqual(gateway(self.db, ModelRole.REFLECTION).model_name, "reflect")

    def test_supervisor_task_cancellation_releases_active_lease(self):
        arbiter = ModelArbiter(self.db)
        request = arbiter.acquire(ModelRole.BACKGROUND, task_id=42)
        arbiter.release_task(42)
        self.assertIsNone(self.db.connection.execute("SELECT * FROM model_lease").fetchone())
        self.assertEqual(self.db.connection.execute(
            "SELECT state FROM model_requests WHERE id=?", (request,)
        ).fetchone()[0], "cancelled")

    def test_background_waits_while_owner_conversation_task_is_queued(self):
        store = TaskStore(self.db)
        conversation = store.enqueue("conversation", {"request_id": 1}, priority=100)
        arbiter = ModelArbiter(self.db, poll_interval=0.005)
        acquired = threading.Event()

        def wait():
            request = arbiter.acquire(ModelRole.CURIOSITY)
            acquired.set()
            arbiter.release(request)

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.05)
        self.assertFalse(acquired.is_set())
        store.claim("conversation", ["conversation"])
        store.transition(conversation, "completed")
        thread.join(2)
        self.assertTrue(acquired.is_set())


if __name__ == "__main__":
    unittest.main()
