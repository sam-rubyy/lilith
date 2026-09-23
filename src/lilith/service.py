"""Always-on, model-idle service and durable chat inbox for detachable clients."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from lilith.config import get_database_path, get_model_name, get_workspace
from lilith.database import Database, utc_now
from lilith.tasks import TaskStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_status (
 id INTEGER PRIMARY KEY CHECK(id=1), pid INTEGER NOT NULL, process_created REAL NOT NULL,
 heartbeat TEXT NOT NULL, state TEXT NOT NULL, stop_requested INTEGER NOT NULL DEFAULT 0,
 model TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 message TEXT NOT NULL, response TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'queued',
 task_id INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS runtime_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS curiosity_sessions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, task_id INTEGER NOT NULL
);
"""
DEFAULTS = {"curiosity_enabled": True, "idle_seconds": 900, "interval_seconds": 3600, "daily_sessions": 6}


class RuntimeState:
    def __init__(self, db):
        self.db, self.store = db, TaskStore(db)
        with db.lock:
            db.connection.executescript(SCHEMA)
            for key, value in DEFAULTS.items():
                db.connection.execute("INSERT OR IGNORE INTO runtime_settings VALUES (?,?)", (key, json.dumps(value)))
            db.connection.commit()

    def settings(self):
        return {r["key"]: json.loads(r["value"]) for r in self.store.rows("SELECT * FROM runtime_settings")}

    def set(self, key, value):
        if key not in DEFAULTS:
            raise ValueError("Unknown runtime setting")
        if key == "curiosity_enabled":
            if type(value) is not bool:
                raise ValueError("Curiosity setting requires true/false")
        elif type(value) is not int or not 1 <= value <= (24 if key == "daily_sessions" else 86400):
            raise ValueError("Invalid scheduler setting")
        with self.store.transaction() as c:
            c.execute("UPDATE runtime_settings SET value=? WHERE key=?", (json.dumps(value), key))
            self.store._event(c, "runtime_setting", {"key": key, "value": value})
        if key == "curiosity_enabled" and value is False:
            self.interrupt_curiosity()

    def interrupt_curiosity(self):
        for task in self.store.rows("SELECT id FROM tasks WHERE origin IN ('self_directed','curiosity') AND state NOT IN ('completed','failed','cancelled','needs_review')"):
            self.store.cancel(task["id"])

    def submit(self, message):
        if not message.strip() or len(message) > 16000:
            raise ValueError("Messages must contain 1–16000 characters")
        with self.store.transaction() as c:
            now = utc_now()
            ident = c.execute("INSERT INTO inbox(created_at,updated_at,message) VALUES (?,?,?)", (now, now, message)).lastrowid
            self.store._event(c, "owner_message_queued", {"request_id": ident})
        self.interrupt_curiosity()
        return ident

    def dispatch(self):
        # Transactionally link every inbox entry to exactly one conversation task.
        with self.store.transaction() as c:
            for ident, message in c.execute("SELECT id,message FROM inbox WHERE state='queued' AND task_id IS NULL ORDER BY id LIMIT 20").fetchall():
                now = utc_now()
                task = c.execute("""INSERT INTO tasks(type,priority,origin,created_at,updated_at,input,resumable,max_attempts,timeout)
                    VALUES ('conversation',100,'owner',?,?,?,0,1,360)""", (now, now, json.dumps({"request_id": ident}))).lastrowid
                c.execute("UPDATE inbox SET task_id=?,updated_at=? WHERE id=?", (task, now, ident))
                self.store._event(c, "conversation_queued", {"request_id": ident, "task_id": task})

    def sync_failures(self):
        with self.store.transaction() as c:
            rows = c.execute("""SELECT i.id,t.state,t.error FROM inbox i JOIN tasks t ON i.task_id=t.id
                WHERE i.state IN ('queued','streaming') AND t.state IN ('failed','cancelled','needs_review')""").fetchall()
            for ident, state, error in rows:
                c.execute("UPDATE inbox SET state=?,error=?,updated_at=? WHERE id=?", (state, error, utc_now(), ident))

    def schedule_curiosity(self, now=None):
        now = now or datetime.now(timezone.utc)
        settings = self.settings()
        if not settings["curiosity_enabled"]:
            return None
        if self.store.rows("SELECT id FROM inbox WHERE state IN ('queued','streaming') LIMIT 1"):
            return None
        if self.store.rows("SELECT id FROM tasks WHERE state NOT IN ('completed','failed','cancelled','needs_review') LIMIT 1"):
            return None
        last = self.store.rows("SELECT created_at FROM inbox ORDER BY id DESC LIMIT 1")
        if not last:
            last = self.store.rows("SELECT timestamp AS created_at FROM chat_messages WHERE role='user' ORDER BY id DESC LIMIT 1")
        if last and (now - datetime.fromisoformat(last[0]["created_at"])).total_seconds() < settings["idle_seconds"]:
            return None
        sessions = self.store.rows("SELECT created_at FROM curiosity_sessions ORDER BY id DESC LIMIT 24")
        if sessions and (now - datetime.fromisoformat(sessions[0]["created_at"])).total_seconds() < settings["interval_seconds"]:
            return None
        if sum(datetime.fromisoformat(r["created_at"]).date() == now.date() for r in sessions) >= settings["daily_sessions"]:
            return None
        # First boot gets an idle grace period instead of immediately loading a model.
        if not sessions and not last:
            started = self.store.rows("SELECT process_created FROM runtime_status WHERE id=1")
            if not started or now.timestamp() - started[0]["process_created"] < settings["idle_seconds"]:
                return None
        task_id = self.store.enqueue("curiosity", {}, priority=5, origin="self_directed", timeout=180)
        with self.store.transaction() as c:
            c.execute("INSERT INTO curiosity_sessions(created_at,task_id) VALUES (?,?)", (now.isoformat(), task_id))
            self.store._event(c, "curiosity_window_started", {"task_id": task_id})
        return task_id

    def status(self):
        rows = self.store.rows("SELECT * FROM runtime_status WHERE id=1")
        if not rows:
            return {"state": "offline", "alive": False}
        row = rows[0]
        import psutil
        try:
            process = psutil.Process(row["pid"])
            alive = abs(process.create_time() - row["process_created"]) < 1 and process.is_running()
        except psutil.Error:
            alive = False
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["heartbeat"])).total_seconds()
        return {**row, "alive": alive, "heartbeat_age": age}


def run_service():
    from lilith.workers import WorkerManager
    from lilith.self_state import SelfState
    import psutil
    db = Database(get_database_path())
    state = RuntimeState(db)
    manager = WorkerManager(db, get_workspace(), get_model_name())
    stopping = False

    def stop(*args):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    try:
        manager.start()
        SelfState(db)
        with state.store.transaction() as c:
            c.execute("INSERT OR REPLACE INTO runtime_status VALUES (1,?,?,?,'idle',0,?)",
                      (os.getpid(), psutil.Process().create_time(), utc_now(), get_model_name()))
        db.audit("service_started", "Persistent runtime online; models are requested only for queued cognition.")
        while not stopping:
            row = state.store.rows("SELECT stop_requested FROM runtime_status WHERE id=1")[0]
            if row["stop_requested"]:
                break
            if manager.error:
                raise RuntimeError(manager.error)
            state.dispatch()
            state.sync_failures()
            state.schedule_curiosity()
            busy = state.store.rows("SELECT id FROM tasks WHERE state IN ('running','planning','researching','verifying','reflecting') LIMIT 1")
            with state.store.transaction() as c:
                c.execute("UPDATE runtime_status SET heartbeat=?,state=? WHERE id=1", (utc_now(), "active" if busy else "idle"))
            time.sleep(0.25)
    finally:
        manager.close()
        state.sync_failures()
        with state.store.transaction() as c:
            c.execute("UPDATE runtime_status SET state='offline',heartbeat=? WHERE id=1 AND pid=?", (utc_now(), os.getpid()))
        db.close()


_SERVICE_PROCESSES = []


def ensure_service(db, *, timeout=12):
    state = RuntimeState(db)
    if state.status()["alive"]:
        return state.status()
    # A legacy console owns the same supervisor lock but has no service heartbeat.
    # Detect it before launching a watchdog that would only retry the conflict.
    from lilith.workers import RuntimeLock
    try:
        probe = RuntimeLock(db.path)
    except RuntimeError:
        raise RuntimeError("An older Lilith console is using this database. Type /quit there, then reopen this dashboard.") from None
    probe.close()
    with state.store.transaction() as c:
        c.execute("UPDATE runtime_status SET stop_requested=0 WHERE id=1")
    env = {**os.environ, "LILITH_DATA_DIR": str(db.path.parent)}
    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
    log_path = db.path.parent / "service.log"
    if log_path.exists() and log_path.stat().st_size > 1048576:
        log_path.replace(log_path.with_suffix(".previous.log"))
    with log_path.open("ab") as log:
        process = subprocess.Popen([sys.executable, "-B", "-m", "lilith.service", "watch"], env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags,
            start_new_session=os.name != "nt")
    _SERVICE_PROCESSES.append(process)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if state.status()["alive"]:
            return state.status()
        if process.poll() is not None:
            raise RuntimeError(f"Service exited ({process.returncode}); see {log_path}. Stop any legacy console runtime first.")
        time.sleep(0.1)
    raise TimeoutError(f"Service is still starting; inspect {log_path} before retrying")


def watch_service():
    """Restart infrastructure failures, never replay non-resumable task side effects."""
    from lilith.workers import RuntimeLock
    path = get_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = RuntimeLock(str(path) + ".watchdog")
    try:
        for attempt in range(4):
            process = subprocess.Popen([sys.executable, "-B", "-m", "lilith.service", "run"],
                stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            code = process.wait()
            db = Database(path)
            state = RuntimeState(db)
            rows = state.store.rows("SELECT stop_requested FROM runtime_status WHERE id=1")
            stop_requested = bool(rows and rows[0]["stop_requested"])
            db.audit("service_process_exit", json.dumps({"exit_code": code, "attempt": attempt + 1}))
            db.close()
            if code == 0 or stop_requested:
                break
            time.sleep(min(30, 5 * (attempt + 1)))
    finally:
        lock.close()


def main():
    parser = argparse.ArgumentParser(description="Lilith persistent runtime")
    parser.add_argument("action", choices=["run", "watch", "start", "stop", "status", "install-startup", "remove-startup"])
    args = parser.parse_args()
    if args.action == "run":
        run_service()
        return
    if args.action == "watch":
        watch_service()
        return
    if args.action in {"install-startup", "remove-startup"}:
        from lilith.startup import install, remove
        print(install() if args.action == "install-startup" else remove())
        return
    db = Database(get_database_path())
    try:
        state = RuntimeState(db)
        if args.action == "start":
            print(json.dumps(ensure_service(db), indent=2))
        elif args.action == "stop":
            with state.store.transaction() as c:
                c.execute("UPDATE runtime_status SET stop_requested=1 WHERE id=1")
            print("Service shutdown requested.")
        else:
            print(json.dumps(state.status(), indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
