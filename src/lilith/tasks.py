"""Durable queue and goal store. Claims and transitions are SQLite transactions."""
import json
from contextlib import contextmanager

from lilith.database import utc_now

ACTIVE = {"planning", "researching", "running", "waiting", "verifying", "reflecting"}
TERMINAL = {"completed", "failed", "cancelled", "needs_review"}
KINDS = {"reflection", "journal", "executive", "capability", "research", "workshop", "workshop_stage", "tool_invocation", "conversation", "curiosity"}
GOAL_TYPES = {"owner", "shared", "self_directed", "maintenance"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, title TEXT NOT NULL,
 description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', priority INTEGER NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, motivation TEXT NOT NULL,
 parent_goal INTEGER REFERENCES goals(id), progress REAL NOT NULL DEFAULT 0,
 next_action TEXT NOT NULL DEFAULT '', resource_budget TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
 id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 priority INTEGER NOT NULL, origin TEXT NOT NULL, goal_id INTEGER REFERENCES goals(id),
 created_at TEXT NOT NULL, started_at TEXT, updated_at TEXT NOT NULL, finished_at TEXT,
 worker TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
 input TEXT NOT NULL, result TEXT, error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
 parent_task_id INTEGER REFERENCES tasks(id), resumable INTEGER NOT NULL DEFAULT 0,
 timeout REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS task_queue ON tasks(state, priority DESC, id);
CREATE UNIQUE INDEX IF NOT EXISTS goal_active_task ON tasks(goal_id)
 WHERE goal_id IS NOT NULL AND state NOT IN ('completed','failed','cancelled','needs_review');
CREATE TABLE IF NOT EXISTS workers (
 id TEXT PRIMARY KEY, task_id INTEGER, heartbeat TEXT NOT NULL, state TEXT NOT NULL
);
"""


class TaskStore:
    def __init__(self, database):
        self.db = database
        with database.lock:
            database.connection.executescript(SCHEMA)
            database.connection.commit()

    @contextmanager
    def transaction(self):
        with self.db.lock:
            c = self.db.connection
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise

    def _event(self, c, event, payload):
        c.execute("INSERT INTO audit_events(timestamp,event_type,actor,message) VALUES (?,?,?,?)",
                  (utc_now(), event, "task_engine", json.dumps(payload)))

    def rows(self, sql, args=()):
        with self.db.lock:
            cur = self.db.connection.execute(sql, args)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def get(self, task_id):
        rows = self.rows("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not rows:
            raise ValueError(f"Unknown task {task_id}")
        task = rows[0]
        task["input"] = json.loads(task["input"])
        task["result"] = json.loads(task["result"]) if task["result"] else None
        return task

    def enqueue(self, kind, payload, *, priority=50, origin="owner", goal_id=None,
                parent_task_id=None, resumable=False, max_attempts=1, timeout=180):
        if kind not in KINDS or not isinstance(payload, dict):
            raise ValueError("Invalid task type or input")
        if not 0 <= priority <= 100 or not 1 <= max_attempts <= 5 or not 1 <= timeout <= 900:
            raise ValueError("Invalid task limits")
        now = utc_now()
        with self.transaction() as c:
            if goal_id is not None:
                goal = c.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
                if not goal or goal[0] != "active":
                    return None
                existing = c.execute("""SELECT id FROM tasks WHERE goal_id=? AND state NOT IN
                    ('completed','failed','cancelled','needs_review')""", (goal_id,)).fetchone()
                if existing:
                    return existing[0]
            cur = c.execute("""INSERT INTO tasks(type,priority,origin,goal_id,created_at,updated_at,
                input,parent_task_id,resumable,max_attempts,timeout) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (kind, priority, origin, goal_id, now, now, json.dumps(payload), parent_task_id,
                 int(resumable), max_attempts, timeout))
            self._event(c, "task_queued", {"task_id": cur.lastrowid, "type": kind})
            return cur.lastrowid

    def claim(self, worker, kinds):
        with self.transaction() as c:
            row = c.execute(f"""SELECT id FROM tasks WHERE state='queued' AND cancel_requested=0
                AND type IN ({','.join('?' for _ in kinds)}) ORDER BY priority DESC,id LIMIT 1""",
                tuple(kinds)).fetchone()
            if row is None:
                return None
            now = utc_now()
            c.execute("""UPDATE tasks SET state='running',worker=?,started_at=?,updated_at=?,
                attempt_count=attempt_count+1 WHERE id=?""", (worker, now, now, row[0]))
            c.execute("INSERT OR REPLACE INTO workers VALUES (?,?,?,?)", (worker, row[0], now, "running"))
            self._event(c, "task_started", {"task_id": row[0], "worker": worker})
        return self.get(row[0])

    def transition(self, task_id, state, *, result=None, error=None):
        if state not in ACTIVE | TERMINAL:
            raise ValueError("Invalid task state")
        with self.transaction() as c:
            row = c.execute("SELECT state,goal_id,cancel_requested FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row[0] not in ACTIVE:
                raise ValueError("Only active tasks can transition")
            if row[2] and state == "completed":
                state = "needs_review"
            now = utc_now()
            c.execute("UPDATE tasks SET state=?,updated_at=?,finished_at=?,result=COALESCE(?,result),error=? WHERE id=?",
                      (state, now, now if state in TERMINAL else None,
                       json.dumps(result) if result is not None else None, error, task_id))
            self._event(c, "task_transition", {"task_id": task_id, "from": row[0], "to": state, "error": error})
            if row[1] and state in TERMINAL:
                c.execute("UPDATE goals SET status=?,progress=?,updated_at=? WHERE id=? AND status='active'",
                          ("completed" if state == "completed" else "needs_review",
                           1 if state == "completed" else 0, now, row[1]))

    def cancel(self, task_id):
        with self.transaction() as c:
            row = c.execute("SELECT state,goal_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("Unknown task")
            if row[0] in TERMINAL:
                return
            c.execute("UPDATE tasks SET cancel_requested=1,updated_at=? WHERE id=?", (utc_now(), task_id))
            if row[0] == "queued":
                c.execute("UPDATE tasks SET state='cancelled',finished_at=? WHERE id=?", (utc_now(), task_id))
                if row[1]:
                    c.execute("UPDATE goals SET status='paused',updated_at=? WHERE id=? AND status='active'", (utc_now(), row[1]))
            self._event(c, "task_cancel_requested", {"task_id": task_id})
        for child in self.rows("SELECT id FROM tasks WHERE parent_task_id=? AND state NOT IN ('completed','failed','cancelled','needs_review')", (task_id,)):
            self.cancel(child["id"])

    def interrupted(self, task_id, reason):
        task = self.get(task_id)
        if task["state"] not in ACTIVE:
            return
        if task["resumable"] and not task["cancel_requested"] and task["attempt_count"] < task["max_attempts"]:
            with self.transaction() as c:
                c.execute("UPDATE tasks SET state='queued',worker=NULL,updated_at=?,error=? WHERE id=?",
                          (utc_now(), reason, task_id))
                self._event(c, "task_retry", {"task_id": task_id, "reason": reason})
        else:
            self.transition(task_id, "cancelled" if task["resumable"] and task["cancel_requested"]
                            else "failed" if task["resumable"] else "needs_review", error=reason)

    def recover(self):
        for row in self.rows("""SELECT id FROM tasks WHERE state NOT IN ('queued','completed','failed','cancelled','needs_review')
                             AND NOT (type='workshop' AND state='waiting')"""):
            self.interrupted(row["id"], "Worker interrupted; recovered on restart")
        with self.transaction() as c:
            c.execute("UPDATE workers SET state='stopped'")

    def goal(self, title, *, kind="owner", description="", priority=50, motivation="Owner request", parent=None):
        if kind not in GOAL_TYPES or not title.strip() or not 0 <= priority <= 100:
            raise ValueError("Invalid goal")
        budget = {"steps": 6, "seconds": 180, "model_calls": 3, "network_bytes": 0, "storage_bytes": 1048576}
        with self.transaction() as c:
            now = utc_now()
            cur = c.execute("""INSERT INTO goals(type,title,description,priority,created_at,updated_at,
                motivation,parent_goal,resource_budget) VALUES (?,?,?,?,?,?,?,?,?)""",
                (kind, title, description, priority, now, now, motivation, parent, json.dumps(budget)))
            self._event(c, "goal_created", {"goal_id": cur.lastrowid, "type": kind})
            return cur.lastrowid

    def schedule_goal(self):
        # The supervisor is the sole scheduler; the partial unique index also prevents duplicate work.
        rows = self.rows("""SELECT * FROM goals g WHERE status='active' AND NOT EXISTS
            (SELECT 1 FROM tasks t WHERE t.goal_id=g.id AND t.state NOT IN
            ('completed','failed','cancelled','needs_review')) ORDER BY priority DESC,id LIMIT 1""")
        if rows:
            g = rows[0]
            budget = json.loads(g["resource_budget"])
            return self.enqueue("executive", {"request": g["title"] + "\n" + g["description"], "budget": budget},
                                priority=g["priority"], origin=g["type"], goal_id=g["id"], timeout=budget["seconds"])
