import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import threading
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from lilith.capabilities import CapabilityBroker, CapabilityError, READ_ONLY, SPECS
from lilith.commands import handle_command
from lilith.database import Database
from lilith.diagnostics import health_snapshot
from lilith.executive import Executive
from lilith.lobotomize import create_backup, clear_database
from lilith.tasks import TaskStore
from lilith.workers import RuntimeLock, WorkerManager, execute_task, reinforce_curiosity_outcome


class FakeGateway:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = 0

    def chat_json(self, messages, schema=None):
        self.calls += 1
        return next(self.replies)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.db = Database(self.root / "state.db")
        self.store = TaskStore(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def broker(self, **kwargs):
        return CapabilityBroker(self.db, self.workspace, allowed=SPECS, **kwargs)

    def test_priority_claim_and_persistence(self):
        low = self.store.enqueue("journal", {"body": "low"}, priority=1)
        high = self.store.enqueue("journal", {"body": "high"}, priority=90)
        self.assertEqual(self.store.claim("one", ["journal"])["id"], high)
        second = Database(self.db.path)
        try:
            self.assertEqual(TaskStore(second).claim("two", ["journal"])["id"], low)
            self.assertIsNone(TaskStore(second).claim("three", ["journal"]))
        finally:
            second.close()

    def test_restart_safe_retry_and_unsafe_review(self):
        safe = self.store.enqueue("capability", {"capability": "filesystem.read", "arguments": {"path": "a"}}, resumable=True, max_attempts=2)
        unsafe = self.store.enqueue("reflection", {})
        self.store.claim("a", ["capability"])
        self.store.claim("b", ["reflection"])
        self.store.recover()
        self.assertEqual(self.store.get(safe)["state"], "queued")
        self.assertEqual(self.store.get(unsafe)["state"], "needs_review")
        self.store.claim("c", ["capability"])
        self.store.recover()
        self.assertEqual(self.store.get(safe)["state"], "failed")

    def test_cancellation_and_terminal_guard(self):
        ident = self.store.enqueue("journal", {})
        self.store.cancel(ident)
        self.assertEqual(self.store.get(ident)["state"], "cancelled")
        self.assertIsNone(self.store.claim("a", ["journal"]))
        with self.assertRaises(ValueError):
            self.store.transition(ident, "running")
        active = self.store.enqueue("journal", {})
        self.store.claim("b", ["journal"])
        self.store.cancel(active)
        self.store.transition(active, "completed")
        self.assertEqual(self.store.get(active)["state"], "needs_review")

    def test_side_effecting_work_cannot_opt_into_replay(self):
        for kind, payload in (("journal", {}), ("reflection", {}),
                              ("capability", {"capability": "filesystem.write"})):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "read-only"):
                self.store.enqueue(kind, payload, resumable=True)
        ident = self.store.enqueue("capability", {"capability": "filesystem.write"})
        self.store.claim("worker", ["capability"])
        # Older databases may already contain an incorrect resumable flag.
        with self.store.transaction() as c:
            c.execute("UPDATE tasks SET resumable=1,max_attempts=2 WHERE id=?", (ident,))
        self.store.recover()
        self.assertEqual(self.store.get(ident)["state"], "needs_review")

    def test_cancelled_parent_cannot_enqueue_late_children(self):
        parent = self.store.enqueue("curiosity", {})
        self.store.claim("parent", ["curiosity"])
        self.store.cancel(parent)
        with self.assertRaisesRegex(ValueError, "interrupted parent"):
            self.store.enqueue("research", {"query": "plants"}, parent_task_id=parent)
        self.assertFalse(self.store.rows("SELECT id FROM tasks WHERE parent_task_id=?", (parent,)))

    def test_interrupted_does_not_overwrite_completed_task(self):
        ident = self.store.enqueue("capability", {"capability": "filesystem.read"},
                                   resumable=True, max_attempts=2)
        self.store.claim("reader", ["capability"])
        self.store.transition(ident, "completed", result={"content": "done"})
        self.store.interrupted(ident, "late exit notification")
        self.assertEqual(self.store.get(ident)["state"], "completed")

    def test_goal_selection_and_no_duplicate(self):
        for kind in ("owner", "shared", "self_directed", "maintenance"):
            self.store.goal(kind, kind=kind, priority=80 if kind == "maintenance" else 10)
        ident = self.store.schedule_goal()
        self.assertEqual(self.store.get(ident)["origin"], "maintenance")
        self.store.schedule_goal()
        self.assertEqual(len(self.store.rows("SELECT * FROM tasks WHERE goal_id=4")), 1)

    def test_files_and_audits(self):
        b = self.broker(task_id=42)
        b.invoke("filesystem.create", {"path": "a.txt", "content": "hello"})
        self.assertEqual(b.invoke("filesystem.read", {"path": "a.txt"})["content"], "hello")
        b.invoke("filesystem.copy", {"source": "a.txt", "destination": "b.txt"})
        b.invoke("filesystem.move", {"source": "b.txt", "destination": "c.txt"})
        b.invoke("filesystem.delete", {"path": "c.txt"})
        self.assertFalse((self.workspace / "c.txt").exists())
        events = self.store.rows("SELECT * FROM audit_events WHERE event_type='capability_completed'")
        self.assertEqual(len(events), 5)
        self.assertEqual(json.loads(events[0]["message"])["task_id"], 42)

    def test_scope_schema_and_denied_audit(self):
        b = self.broker()
        for args in ({"path": "../outside.txt", "content": "bad"}, {"path": True, "content": "bad"},
                     {"path": "ok", "content": "bad", "extra": 1}):
            with self.assertRaises(CapabilityError):
                b.invoke("filesystem.write", args)
        with self.assertRaises(CapabilityError):
            CapabilityBroker(self.db, self.workspace).invoke("filesystem.read", {"path": "a"})
        self.assertEqual(len(self.store.rows("SELECT id FROM audit_events WHERE event_type='capability_failed'")), 4)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_capability_audit_redacts_environment_and_content(self):
        broker = CapabilityBroker(self.db, self.workspace, allowed={"terminal.run", "filesystem.write"})
        broker.invoke("filesystem.write", {"path": "note.txt", "content": "private body"})
        broker.invoke("terminal.run", {
            "command": sys.executable, "arguments": ["-c", "print('ok')"],
            "working_directory": ".", "timeout": 5,
            "environment": {"API_TOKEN": "do-not-store"}, "expected_effect": "print",
        })
        messages = "\n".join(row["message"] for row in self.store.rows(
            "SELECT message FROM audit_events WHERE event_type LIKE 'capability_%'"
        ))
        self.assertNotIn("do-not-store", messages)
        self.assertNotIn("private body", messages)
        self.assertIn("[redacted]", messages)

    def test_malformed_capability_arguments_are_audited(self):
        for args in (None, [], "invalid", {1: "invalid"}):
            with self.subTest(args=args), self.assertRaises(CapabilityError):
                self.broker().invoke("filesystem.read", args)
        self.assertEqual(len(self.store.rows(
            "SELECT id FROM audit_events WHERE event_type='capability_requested'"
        )), 4)
        self.assertEqual(len(self.store.rows(
            "SELECT id FROM audit_events WHERE event_type='capability_failed'"
        )), 4)

    def test_storage_limit_and_no_overwrite_copy(self):
        b = self.broker(storage_limit=4)
        with self.assertRaises(CapabilityError):
            b.invoke("filesystem.write", {"path": "large", "content": "12345"})
        self.assertFalse((self.workspace / "large").exists())
        (self.workspace / "one").write_text("1")
        (self.workspace / "two").write_text("2")
        with self.assertRaises(CapabilityError):
            b.invoke("filesystem.copy", {"source": "one", "destination": "two"})
        self.assertEqual((self.workspace / "two").read_text(), "2")

    def test_symlink_escape_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        link = self.workspace / "outside-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        with self.assertRaises(CapabilityError):
            self.broker().invoke("filesystem.read", {"path": "outside-link/secret.txt"})

    def test_terminal_literal_arguments_and_exit(self):
        result = self.broker().invoke("terminal.run", {"command": sys.executable,
            "arguments": ["-c", "import sys; print(sys.argv[1]); sys.exit(3)", "hello; echo injected"],
            "working_directory": ".", "timeout": 5, "environment": {}, "expected_effect": "Print literal argument"})
        self.assertEqual(result["returncode"], 3)
        self.assertEqual(result["output"].strip(), "hello; echo injected")

    def test_terminal_output_budget_kills_fast_writer(self):
        with self.assertRaisesRegex(CapabilityError, "output budget"):
            self.broker().invoke("terminal.run", {
                "command": sys.executable,
                "arguments": ["-c", "import sys; sys.stdout.write('x' * 2000000)"],
                "working_directory": ".", "timeout": 10, "environment": {},
                "expected_effect": "Exercise output bound",
            })

    def test_git_status(self):
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "example.txt").write_text("hello")
        result = self.broker().invoke("git.status", {"working_directory": "."})
        self.assertEqual(result["returncode"], 0)
        self.assertIn("example.txt", result["output"])

    def test_executive_plan_review_evidence_and_journal(self):
        (self.workspace / "note.txt").write_text("actual evidence")
        goal = self.store.goal("Read note.txt")
        ident = self.store.schedule_goal()
        task = self.store.claim("executive", ["executive"])
        step = {"capability": "filesystem.read", "arguments": {"path": "note.txt"}, "expected": "Read note"}
        gateway = FakeGateway([{"steps": [step], "needs_review": False}, {"approved": True},
                               {"completed": True, "summary": "actual evidence"}])
        Executive(self.db, self.store, gateway, self.workspace).run(task)
        result = self.store.get(ident)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["result"]["evidence"][0]["result"]["content"], "actual evidence")
        self.assertEqual(self.store.rows("SELECT status FROM goals WHERE id=?", (goal,))[0]["status"], "completed")
        self.assertEqual(gateway.calls, 3)
        self.assertEqual(len(self.store.rows("SELECT * FROM tasks WHERE type='journal'")), 1)

    def test_reviewer_rejects_without_actions(self):
        ident = self.store.enqueue("executive", {"request": "change files"})
        task = self.store.claim("executive", ["executive"])
        gateway = FakeGateway([{"steps": [], "needs_review": True}, {"approved": False, "reason": "No permission"}])
        Executive(self.db, self.store, gateway, self.workspace).run(task)
        self.assertEqual(self.store.get(ident)["state"], "needs_review")
        self.assertFalse(self.store.rows("SELECT * FROM audit_events WHERE event_type='capability_requested'"))

    def test_model_cannot_grant_write(self):
        ident = self.store.enqueue("executive", {"request": "write"})
        task = self.store.claim("executive", ["executive"])
        gateway = FakeGateway([{"steps": [], "needs_review": False}, {"approved": True, "steps": [
            {"capability": "filesystem.write", "arguments": {"path": "pwned", "content": "x"}}]}])
        with self.assertRaises(ValueError):
            Executive(self.db, self.store, gateway, self.workspace).run(task)
        self.assertFalse((self.workspace / "pwned").exists())

    def test_step_budget(self):
        ident = self.store.enqueue("executive", {"request": "read", "budget": {"steps": 1}})
        task = self.store.claim("executive", ["executive"])
        with self.assertRaises(ValueError):
            Executive(self.db, self.store, FakeGateway([{"steps": [{}, {}]}]), self.workspace).run(task)

    def test_reflection_worker_applies_and_queues_journal(self):
        ident = self.store.enqueue("reflection", {"user_message": "hi", "assistant_response": "hello"})
        self.store.claim("reflection", ["reflection"])
        with patch("lilith.workers.gateway", return_value=FakeGateway([{}])):
            execute_task(str(self.db.path), ident, str(self.workspace), "test")
        self.assertEqual(self.store.get(ident)["state"], "completed")
        self.assertEqual(len(self.store.rows("SELECT * FROM tasks WHERE type='journal'")), 1)

    def test_spawned_worker_end_to_end(self):
        manager = WorkerManager(self.db, self.workspace, "unused", poll_interval=0.05)
        ident = self.store.enqueue("capability", {"capability": "filesystem.write",
                                                  "arguments": {"path": "spawn.txt", "content": "from worker"}})
        manager.start()
        try:
            self.wait_terminal(ident)
            self.assertEqual(self.store.get(ident)["state"], "completed", self.store.get(ident))
            self.assertEqual((self.workspace / "spawn.txt").read_text(), "from worker")
        finally:
            manager.close()
        self.assertTrue(self.store.rows("SELECT * FROM workers"))
        self.assertIsNone(manager.error)

    def test_worker_failure_keeps_bounded_diagnostic_log(self):
        manager = WorkerManager(self.db, self.workspace, "unused", poll_interval=0.05)
        ident = self.store.enqueue("reflection", {})
        manager.start()
        try:
            self.wait_terminal(ident)
        finally:
            manager.close()
        task = self.store.get(ident)
        self.assertEqual(task["state"], "needs_review")
        log_path = Path(task["worker_log"])
        self.assertTrue(log_path.is_file())
        log = log_path.read_text(encoding="utf-8")
        self.assertIn("Traceback", log)
        self.assertIn("ReflectionEngine.reflect", log)
        self.assertLessEqual(log_path.stat().st_size, 1048576)

    def test_curiosity_reinforcement_requires_research_outcome(self):
        parent_id = self.store.enqueue("curiosity", {})
        self.store.claim("curiosity", ["curiosity"])
        self.store.transition(parent_id, "completed", result={"topic": "Botany"})
        research_id = self.store.enqueue(
            "research", {"query": "plants"}, origin="curiosity", parent_task_id=parent_id
        )
        research = self.store.get(research_id)
        self.assertEqual(self.db.get_interests(), [])
        self.assertFalse(reinforce_curiosity_outcome(self.db, self.store, research, {"claims": []}))
        self.assertEqual(self.db.get_interests(), [])
        self.assertTrue(reinforce_curiosity_outcome(
            self.db, self.store, research, {"claims": [{"text": "Plants respond to light."}]}
        ))
        self.assertEqual(self.db.get_interests()[0]["topic"], "Botany")

    def wait_terminal(self, ident, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.store.get(ident)["state"] in {"completed", "failed", "cancelled", "needs_review"}:
                return
            time.sleep(0.05)
        self.fail(f"Task did not finish: {self.store.get(ident)}")

    def test_spawned_timeout_needs_review(self):
        ident = self.store.enqueue("capability", {"capability": "terminal.run", "arguments": {
            "command": sys.executable, "arguments": ["-c", "import time; time.sleep(30)"],
            "working_directory": ".", "timeout": 60, "environment": {}, "expected_effect": "sleep"}}, timeout=1)
        manager = WorkerManager(self.db, self.workspace, "unused", poll_interval=0.05)
        manager.start()
        try:
            self.wait_terminal(ident)
            self.assertEqual(self.store.get(ident)["state"], "needs_review")
        finally:
            manager.close()

    def test_active_cancellation_stops_command(self):
        import psutil
        ident = self.store.enqueue("capability", {"capability": "terminal.run", "arguments": {
            "command": sys.executable, "arguments": ["-c", "import os,time,pathlib; pathlib.Path('pid.txt').write_text(str(os.getpid())); time.sleep(30)"],
            "working_directory": ".", "timeout": 60, "environment": {}, "expected_effect": "Run cancellable test sleeper"}})
        manager = WorkerManager(self.db, self.workspace, "unused", poll_interval=0.05)
        manager.start()
        try:
            deadline = time.monotonic() + 5
            while not (self.workspace / "pid.txt").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.workspace / "pid.txt").exists())
            pid = int((self.workspace / "pid.txt").read_text())
            self.store.cancel(ident)
            self.wait_terminal(ident)
            self.assertEqual(self.store.get(ident)["state"], "needs_review")
            self.assertFalse(psutil.pid_exists(pid))
        finally:
            manager.close()

    def test_runtime_lock(self):
        lock = RuntimeLock(self.db.path)
        try:
            with self.assertRaises(RuntimeError):
                RuntimeLock(self.db.path)
        finally:
            lock.close()
        RuntimeLock(self.db.path).close()

    def test_wal_backup_and_reset_new_tables(self):
        self.store.goal("test")
        backup = create_backup(self.db.path)
        with contextlib.closing(sqlite3.connect(backup)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM goals").fetchone()[0], 1)
        clear_database(self.db.path)
        self.assertFalse(self.store.rows("SELECT * FROM goals"))

    def test_commands_and_invalid_input(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(handle_command('/run filesystem.write {"path":"a","content":"b"}', self.store, None))
            handle_command("/goal owner learn", self.store, None)
            handle_command("/cancel bad", self.store, None)
        self.assertIn("Command error", output.getvalue())
        self.assertEqual(len(self.store.rows("SELECT * FROM tasks")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM goals")), 1)

    def test_health_snapshot_exposes_stabilization_state(self):
        snapshot = health_snapshot(
            self.store, runtime={"state": "test"}, settings={"curiosity_enabled": False}
        )
        for key in ("service", "model", "model_queue", "active_model_role", "worker_lanes",
                    "current_tasks", "stale_workers", "database_schema_version", "wal_status",
                    "workspace", "counts", "recent_worker_failures", "curiosity_enabled",
                    "last_curiosity_session", "conversation_performance"):
            self.assertIn(key, snapshot)
        self.assertEqual(snapshot["wal_status"], "wal")

    def test_health_does_not_report_stopped_workers_as_stale(self):
        with self.store.transaction() as c:
            for name, state in (("stopped-worker", "stopped"), ("stalled-worker", "running")):
                c.execute("INSERT INTO workers(id,task_id,heartbeat,state) VALUES (?,NULL,?,?)",
                          (name, "2000-01-01T00:00:00+00:00", state))
        stale = health_snapshot(self.store)["stale_workers"]
        self.assertEqual([row["id"] for row in stale], ["stalled-worker"])

    def test_process_inspect_and_stop(self):
        result = self.broker().invoke("process.start", {"command": sys.executable,
            "arguments": ["-c", "import time; time.sleep(30)"], "working_directory": ".",
            "expected_effect": "Start test sleeper"})
        import psutil
        try:
            info = self.broker().invoke("process.inspect", {"pid": result["pid"]})
            self.assertEqual(info["pid"], result["pid"])
            self.broker().invoke("process.stop", {"pid": result["pid"]})
            self.assertFalse(psutil.pid_exists(result["pid"]))
        finally:
            if psutil.pid_exists(result["pid"]):
                psutil.Process(result["pid"]).kill()

    def test_desktop_adapters_without_operating_desktop(self):
        from unittest.mock import MagicMock
        gui = MagicMock()
        gui.onScreen.return_value = True
        gui.KEYBOARD_KEYS = ["ctrl", "a"]
        # Exercise adapter dispatch/storage without importing optional desktop packages.
        gui.screenshot.return_value.save.side_effect = lambda output, **kwargs: output.write(b"fake PNG")
        with patch.dict(sys.modules, {"pyautogui": gui}):
            b = self.broker()
            b.invoke("desktop.click", {"x": 4, "y": 4})
            b.invoke("desktop.type", {"text": "hello"})
            b.invoke("desktop.keypress", {"keys": ["ctrl", "a"]})
            b.invoke("desktop.scroll", {"amount": 3})
            result = b.invoke("desktop.screenshot", {"path": "screen.png"})
            self.assertTrue(Path(result["path"]).exists())
            gui.click.assert_called_once_with(4, 4, button="left")
            gui.hotkey.assert_called_once_with("ctrl", "a")
            with self.assertRaises(CapabilityError):
                b.invoke("desktop.screenshot", {"path": "screen.png"})

    def test_paused_goal_survives_running_task_cancel(self):
        goal = self.store.goal("work")
        ident = self.store.schedule_goal()
        self.store.claim("a", ["executive"])
        with contextlib.redirect_stdout(io.StringIO()):
            handle_command(f"/goal-state {goal} paused", self.store, None)
        self.store.interrupted(ident, "cancelled")
        self.assertEqual(self.store.rows("SELECT status FROM goals")[0]["status"], "paused")
        self.assertIsNone(self.store.schedule_goal())

    def test_chat_responsive_while_reflecting(self):
        entered = threading.Event()
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if isinstance(data.get("format"), dict) and "action" in data["format"].get("properties", {}):
                    content = json.dumps({"action": "chat"})
                elif (data.get("format") == "json" or
                      (isinstance(data.get("format"), dict) and
                       "memories" in data["format"].get("properties", {}))):
                    entered.set()
                    release.wait(10)
                    content = "{}"
                else:
                    content = "Test reply"
                encoded = json.dumps({"message": {"content": content}, "done": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {**os.environ, "LILITH_DATA_DIR": str(self.root / "chat"), "LILITH_WORKSPACE": str(self.workspace),
               "LILITH_OLLAMA_URL": f"http://127.0.0.1:{server.server_port}"}
        proc = subprocess.Popen([sys.executable, "-B", "-u", "-m", "lilith.main"], env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True)
        reader.start()

        def await_line(fragment):
            deadline = time.monotonic() + 20
            captured = []
            while time.monotonic() < deadline:
                try:
                    line = lines.get(timeout=0.1)
                    captured.append(line)
                    if fragment in line:
                        return
                except queue.Empty:
                    pass
            self.fail(f"Did not receive {fragment}: {captured}")

        try:
            await_line("runtime online")
            proc.stdin.write("hello\n")
            proc.stdin.flush()
            await_line("Reflection queued")
            if not entered.wait(5):
                with contextlib.closing(sqlite3.connect(self.root / "chat" / "lilith.db")) as c:
                    details = c.execute("SELECT state,error FROM tasks").fetchall()
                    audits = c.execute("SELECT event_type,message FROM audit_events ORDER BY id DESC LIMIT 6").fetchall()
                logs = []
                while not lines.empty():
                    logs.append(lines.get_nowait())
                self.fail(f"Reflection worker never reached the model: {details}, {audits}, {logs}")
            proc.stdin.write("second message\n")
            proc.stdin.flush()
            await_line("Reflection queued")
            self.assertFalse(release.is_set())
            release.set()
            proc.stdin.write("/quit\n")
            proc.stdin.flush()
            proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 0)
        finally:
            release.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdin.close()
            reader.join(timeout=2)
            proc.stdout.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
