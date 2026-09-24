"""Central model roles and durable priority arbitration for inference only."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json
import threading
import time
import uuid

from lilith.config import get_role_model
from lilith.database import utc_now
from lilith.model_gateway import OllamaGateway


class ModelRole(StrEnum):
    CONVERSATION = "conversation"
    ROUTER = "router"
    OWNER_EXECUTIVE = "owner_executive"
    OWNER_RESEARCH = "owner_research"
    OWNER_WORKSHOP = "owner_workshop"
    REFLECTION = "reflection"
    BACKGROUND = "background"
    CURIOSITY = "curiosity"
    MAINTENANCE = "maintenance"


PRIORITY = {
    ModelRole.CONVERSATION: 100,
    ModelRole.ROUTER: 95,
    ModelRole.OWNER_EXECUTIVE: 80,
    ModelRole.OWNER_RESEARCH: 75,
    ModelRole.OWNER_WORKSHOP: 70,
    ModelRole.REFLECTION: 40,
    ModelRole.BACKGROUND: 30,
    ModelRole.CURIOSITY: 10,
    ModelRole.MAINTENANCE: 5,
}


class ModelArbiter:
    def __init__(self, database, *, lease_seconds=30, poll_interval=0.05):
        self.db = database
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval

    def _event(self, connection, event, payload):
        connection.execute(
            "INSERT INTO audit_events(timestamp,event_type,actor,message) VALUES (?,?,?,?)",
            (utc_now(), event, "model_arbiter", json.dumps(payload)),
        )

    def _expire_stale(self, connection, now):
        lease = connection.execute(
            "SELECT request_id,role,expires_at FROM model_lease WHERE id=1"
        ).fetchone()
        if lease and lease[2] <= now:
            connection.execute(
                "UPDATE model_requests SET state='expired',released_at=? WHERE id=? AND state='active'",
                (now, lease[0]),
            )
            connection.execute("DELETE FROM model_lease WHERE id=1")
            self._event(connection, "model_lease_expired", {"request_id": lease[0], "role": lease[1]})

    def acquire(self, role: ModelRole, *, task_id=None, check=None):
        request_id = uuid.uuid4().hex
        created = utc_now()
        with self.db.lock:
            self.db.connection.execute(
                "INSERT INTO model_requests(id,role,priority,task_id,created_at,state) VALUES (?,?,?,?,?,'waiting')",
                (request_id, role.value, PRIORITY[role], task_id, created),
            )
            self.db.connection.execute("""DELETE FROM model_requests WHERE state NOT IN ('waiting','active')
                AND rowid NOT IN (SELECT rowid FROM model_requests ORDER BY rowid DESC LIMIT 1000)""")
            self._event(self.db.connection, "model_request_queued", {
                "request_id": request_id, "role": role.value, "priority": PRIORITY[role], "task_id": task_id,
            })
            self.db.connection.commit()
        try:
            while True:
                if check:
                    check()
                now = utc_now()
                expires = (datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)).isoformat()
                with self.db.lock:
                    connection = self.db.connection
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        self._expire_stale(connection, now)
                        lease = connection.execute("SELECT request_id FROM model_lease WHERE id=1").fetchone()
                        first = connection.execute(
                            """SELECT id FROM model_requests WHERE state='waiting'
                               ORDER BY priority DESC,created_at,id LIMIT 1"""
                        ).fetchone()
                        owner_conversation = connection.execute("""SELECT 1 FROM tasks
                            WHERE type='conversation' AND state NOT IN
                            ('completed','failed','cancelled','needs_review') LIMIT 1""").fetchone()
                        owner_inbox = connection.execute("""SELECT 1 FROM inbox
                            WHERE state IN ('queued','streaming') LIMIT 1""").fetchone()
                        blocked_by_owner = PRIORITY[role] < PRIORITY[ModelRole.ROUTER] and (owner_conversation or owner_inbox)
                        if lease is None and first and first[0] == request_id and not blocked_by_owner:
                            connection.execute(
                                "INSERT INTO model_lease VALUES (1,?,?,?,?,?)",
                                (request_id, role.value, now, now, expires),
                            )
                            connection.execute(
                                "UPDATE model_requests SET state='active',acquired_at=? WHERE id=?", (now, request_id)
                            )
                            self._event(connection, "model_lease_acquired", {
                                "request_id": request_id, "role": role.value, "task_id": task_id,
                            })
                            connection.commit()
                            return request_id
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                time.sleep(self.poll_interval)
        except BaseException:
            self.release(request_id, state="cancelled")
            raise

    def heartbeat(self, request_id):
        now = utc_now()
        expires = (datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)).isoformat()
        with self.db.lock:
            self.db.connection.execute(
                "UPDATE model_lease SET heartbeat=?,expires_at=? WHERE id=1 AND request_id=?",
                (now, expires, request_id),
            )
            self.db.connection.commit()

    def release(self, request_id, *, state="completed"):
        with self.db.lock:
            connection = self.db.connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT role,task_id,state FROM model_requests WHERE id=?", (request_id,)
                ).fetchone()
                connection.execute("DELETE FROM model_lease WHERE id=1 AND request_id=?", (request_id,))
                connection.execute(
                    "UPDATE model_requests SET state=?,released_at=? WHERE id=? AND state IN ('waiting','active')",
                    (state, utc_now(), request_id),
                )
                if row and row[2] in {"waiting", "active"}:
                    self._event(connection, "model_lease_released", {
                        "request_id": request_id, "role": row[0], "task_id": row[1], "state": state,
                    })
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def release_task(self, task_id, *, state="cancelled"):
        """Supervisor cleanup for a worker killed before its finally block runs."""
        with self.db.lock:
            connection = self.db.connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT id,role FROM model_requests WHERE task_id=? AND state IN ('waiting','active')",
                    (task_id,),
                ).fetchall()
                for request_id, role in rows:
                    connection.execute("DELETE FROM model_lease WHERE request_id=?", (request_id,))
                    connection.execute(
                        "UPDATE model_requests SET state=?,released_at=? WHERE id=?", (state, utc_now(), request_id)
                    )
                    self._event(connection, "model_lease_released", {
                        "request_id": request_id, "role": role, "task_id": task_id, "state": state,
                    })
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def recover(self):
        """Called only by a supervisor holding the exclusive runtime lock."""
        with self.db.lock:
            connection = self.db.connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT id,role,task_id FROM model_requests WHERE state IN ('waiting','active')"
                ).fetchall()
                for request_id, role, task_id in rows:
                    self._event(connection, "model_lease_released", {
                        "request_id": request_id, "role": role, "task_id": task_id,
                        "state": "cancelled", "reason": "runtime_restart",
                    })
                connection.execute("""UPDATE model_requests SET state='cancelled',released_at=?
                    WHERE state IN ('waiting','active')""", (utc_now(),))
                connection.execute("DELETE FROM model_lease")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def lease(self, role, *, task_id=None, check=None):
        request_id = self.acquire(role, task_id=task_id, check=check)
        stopped = threading.Event()

        def maintain():
            while not stopped.wait(max(1, self.lease_seconds / 3)):
                self.heartbeat(request_id)

        thread = threading.Thread(target=maintain, name="model-lease-heartbeat", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1)
            self.release(request_id)


class ArbitratedGateway:
    def __init__(self, database, role, model=None, *, task_id=None, check=None, base_url=None):
        self.role = ModelRole(role)
        role_config = {
            ModelRole.OWNER_EXECUTIVE: "reasoning", ModelRole.BACKGROUND: "reasoning",
            ModelRole.CURIOSITY: "reasoning", ModelRole.MAINTENANCE: "reasoning",
            ModelRole.OWNER_RESEARCH: "research", ModelRole.OWNER_WORKSHOP: "workshop",
        }.get(self.role, self.role.value)
        self.inner = OllamaGateway(model or get_role_model(role_config), base_url=base_url)
        self.arbiter = ModelArbiter(database)
        self.task_id = task_id
        self.check = check

    @property
    def model_name(self):
        return self.inner.model_name

    def chat(self, messages):
        with self.arbiter.lease(self.role, task_id=self.task_id, check=self.check):
            return self.inner.chat(messages)

    def chat_json(self, messages, schema=None):
        with self.arbiter.lease(self.role, task_id=self.task_id, check=self.check):
            return self.inner.chat_json(messages, schema=schema)

    def chat_stream(self, messages):
        with self.arbiter.lease(self.role, task_id=self.task_id, check=self.check):
            yield from self.inner.chat_stream(messages)


def gateway(database, role, model=None, *, task_id=None, check=None, base_url=None):
    return ArbitratedGateway(database, role, model, task_id=task_id, check=check, base_url=base_url)
