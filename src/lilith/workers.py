"""One supervisor, bounded spawned workers, isolated connections, persistent heartbeats."""
from dataclasses import asdict
import json
import os
import subprocess
import sys
from pathlib import Path
import threading
import time
import traceback
import uuid

from lilith.capabilities import CapabilityBroker, shell_enabled
from lilith.database import Database, utc_now
from lilith.executive import Executive
from lilith.models import ModelRole, gateway
from lilith.models import ModelArbiter
from lilith.reflection import ReflectionEngine
from lilith.self_state import SelfState
from lilith.tasks import ACTIVE, TaskStore
from lilith.research import Research, PublicHTTP
from lilith.tool_registry import ToolRegistry
from lilith.workshop import Workshop, start_workshop, reconcile_workshops
from lilith.redaction import error_text, redact_text
from lilith.worker_logs import WorkerLog


def safe_traceback():
    return redact_text(traceback.format_exc())


class RuntimeLock:
    """OS lock prevents a second supervisor from recovering live tasks."""
    def __init__(self, path):
        self.file = open(str(path) + ".runtime.lock", "a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Another Lilith runtime is using this database") from None

    def close(self):
        self.file.close()


def execute_task(database_path, task_id, workspace, model):
    db = Database(Path(database_path))
    store = TaskStore(db)
    task = store.get(task_id)
    try:
        if task["cancel_requested"]:
            store.interrupted(task_id, "Cancelled before execution")
            return
        payload = task["input"]
        if task["type"] == "conversation":
            from lilith.service import RuntimeState
            from lilith.conversation import Conversation
            RuntimeState(db)
            request_id = payload["request_id"]
            row = store.rows("SELECT * FROM inbox WHERE id=?", (request_id,))[0]
            with store.transaction() as c:
                c.execute("UPDATE inbox SET state='streaming',updated_at=? WHERE id=?", (utc_now(), request_id))
            chunks, last_write = [], [0.0]
            def check():
                if store.get(task_id)["cancel_requested"]:
                    raise RuntimeError("Reply cancelled")
            def token(chunk):
                chunks.append(chunk)
                if time.monotonic() - last_write[0] >= 0.08:
                    with store.transaction() as c:
                        c.execute("UPDATE inbox SET response=?,updated_at=? WHERE id=?", ("".join(chunks), utc_now(), request_id))
                    last_write[0] = time.monotonic()
            try:
                response, reflection_id = Conversation(
                    db,
                    gateway(db, ModelRole.CONVERSATION, task_id=task_id, check=check),
                    gateway(db, ModelRole.ROUTER, task_id=task_id, check=check),
                ).reply(row["message"], token, check=check)
                with store.transaction() as c:
                    c.execute("UPDATE inbox SET response=?,state='completed',updated_at=? WHERE id=?", (response, utc_now(), request_id))
                result = {"request_id": request_id, "reflection_task_id": reflection_id}
            except Exception as error:
                with store.transaction() as c:
                    c.execute("UPDATE inbox SET response=?,state='needs_review',error=?,updated_at=? WHERE id=?",
                              ("".join(chunks), error_text(error), utc_now(), request_id))
                raise
        elif task["type"] == "curiosity":
            interests = db.get_interests(limit=8)
            recent_topics = []
            for row in store.rows("""SELECT result FROM tasks WHERE type='curiosity'
                                      AND state='completed' AND result IS NOT NULL
                                      ORDER BY id DESC LIMIT 8"""):
                try:
                    topic = json.loads(row["result"]).get("topic")
                    if isinstance(topic, str):
                        recent_topics.append(topic)
                except (TypeError, json.JSONDecodeError):
                    pass
            proposal = gateway(db, ModelRole.CURIOSITY, task_id=task_id).chat_json([
                {"role": "system", "content": "Choose one small public research question you would like to explore during an idle curiosity window. "
                 "Base it on recorded interests; if none exist, choose a modest topic about software, nature, art, or science. "
                 "Avoid private owner details. Strongly prefer a topic not explored recently; recent selection is a penalty, not evidence of fascination. "
                 "Return topic, question (a short search query), and motivation (one sentence explaining the choice)."},
                {"role": "user", "content": json.dumps({"interests": interests, "recent_topics": recent_topics})},
            ], schema={"type": "object", "properties": {key: {"type": "string"} for key in ("topic", "question", "motivation")},
                       "required": ["topic", "question", "motivation"], "additionalProperties": False})
            if any(not isinstance(proposal.get(key), str) or not 1 <= len(proposal[key]) <= limit
                   for key, limit in (("topic", 100), ("question", 500), ("motivation", 500))):
                raise ValueError("Invalid curiosity proposal")
            normalized_recent = {" ".join(topic.lower().split()) for topic in recent_topics}
            if " ".join(proposal["topic"].lower().split()) in normalized_recent:
                raise ValueError("Curiosity selected a topic explored too recently")
            research_id = store.enqueue("research", {"query": proposal["question"]}, priority=5,
                                        origin="curiosity", parent_task_id=task_id, timeout=240)
            db.journal(title="A question for my quiet time", body=proposal["motivation"] + "\n\n" + proposal["question"])
            result = {**proposal, "research_task_id": research_id}
        elif task["type"] == "reflection":
            store.transition(task_id, "reflecting")
            result = asdict(ReflectionEngine(
                db, SelfState(db), gateway(db, ModelRole.REFLECTION, task_id=task_id)
            ).reflect(**payload))
            store.enqueue("journal", {"title": f"Interaction reflection #{task_id}", "body": str(result)},
                          origin="reflection", priority=10, parent_task_id=task_id)
        elif task["type"] == "journal":
            db.journal(body=payload["body"], title=payload.get("title"))
            result = {"written": True}
        elif task["type"] == "executive":
            role = ModelRole.OWNER_EXECUTIVE if task["origin"] in {"owner", "conversation"} else ModelRole.BACKGROUND
            Executive(db, store, gateway(db, role, task_id=task_id), workspace).run(task)
            return
        elif task["type"] == "research":
            def check():
                if store.get(task_id)["cancel_requested"]:
                    raise RuntimeError("Research cancelled")
            role = ModelRole.OWNER_RESEARCH if task["origin"] in {"owner", "conversation"} else ModelRole.BACKGROUND
            if task["origin"] == "curiosity":
                role = ModelRole.CURIOSITY
            result = Research(db, store, gateway(db, role, task_id=task_id, check=check),
                              http=PublicHTTP(check=check)).run(task)
            reinforce_curiosity_outcome(db, store, task, result)
        elif task["type"] == "workshop":
            start_workshop(store, task)
            return
        elif task["type"] == "workshop_stage":
            role = ModelRole.OWNER_WORKSHOP if task["origin"] in {"owner", "conversation"} else ModelRole.BACKGROUND
            result = Workshop(db, store, gateway(db, role, task_id=task_id), workspace).run_stage(task)
            if store.get(task_id)["state"] not in ACTIVE:
                return
        elif task["type"] == "tool_invocation":
            result = ToolRegistry(db, store).invoke(payload["name"], payload["data"], task_id=task_id,
                                                  allow_shell=shell_enabled(), workspace=workspace)
        elif task["type"] == "capability":
            result = CapabilityBroker(db, workspace, allowed=[payload["capability"]], task_id=task_id,
                                      storage_limit=16 * 1048576).invoke(payload["capability"], payload["arguments"])
            if isinstance(result, dict) and result.get("returncode", 0) != 0:
                store.transition(task_id, "needs_review", result=result, error="Command returned a nonzero exit code")
                return
        else:
            raise ValueError("Unknown worker type")
        store.transition(task_id, "completed", result=result)
    except Exception as error:
        # The supervisor captures this bounded stream in the task's diagnostic log.
        if store.get(task_id).get("worker_log"):
            print(safe_traceback(), file=sys.stderr, end="")
        store.interrupted(task_id, f"{type(error).__name__}: {error}")
    finally:
        db.close()


def reinforce_curiosity_outcome(db, store, task, result):
    """Reward only a completed, cited curiosity result—not topic selection."""
    if task.get("origin") != "curiosity" or not result.get("claims") or not task.get("parent_task_id"):
        return False
    parent = store.get(task["parent_task_id"])
    topic = (parent.get("result") or {}).get("topic")
    if not isinstance(topic, str) or not topic.strip():
        return False
    db.adjust_interest(topic, 0.02)
    db.audit("curiosity_interest_adjusted", json.dumps({
        "topic": topic, "delta": 0.02, "research_task_id": task["id"], "reason": "completed_cited_research",
    }), "curiosity")
    return True


class WorkerManager:
    def __init__(self, database, workspace, model, *, poll_interval=0.25):
        self.db = database
        self.store = TaskStore(database)
        self.workspace = str(Path(workspace).resolve())
        self.model = model
        self.interval = poll_interval
        self.stop_event = threading.Event()
        self.children = {}
        self.logs = {}
        self.thread = None
        self.lock = None
        self.error = None

    def start(self):
        self.lock = RuntimeLock(self.db.path)
        try:
            self._recover_processes()
            ModelArbiter(self.db).recover()
            self.store.recover()
            self.thread = threading.Thread(target=self._loop, name="lilith-supervisor", daemon=True)
            self.thread.start()
        except BaseException:
            self.lock.close()
            raise

    def _loop(self):
        try:
            while not self.stop_event.is_set():
                self.tick()
                self.stop_event.wait(self.interval)
        except Exception as error:
            self.error = error_text(error)
            self.db.audit("supervisor_failed", str(error), "worker_manager")
        finally:
            for lane, (proc, task, started) in list(self.children.items()):
                self._terminate(proc)
                self._finish_log(task, proc, "runtime shutdown")
                ModelArbiter(self.db).release_task(task["id"])
                self.store.interrupted(task["id"], "Runtime shutdown interrupted worker")
                self._heartbeat(task, "stopped")
                del self.children[lane]

    def _recover_processes(self):
        """Stop only recorded processes whose creation time still matches the PID."""
        import psutil
        for row in self.store.rows("SELECT * FROM workers WHERE state='running' AND pid IS NOT NULL"):
            try:
                process = psutil.Process(row["pid"])
                if process.create_time() != row["process_created"]:
                    self.db.audit("worker_identity_mismatch", json.dumps({"worker": row["id"], "pid": row["pid"]}))
                    continue
                if process.pid in {os.getpid(), os.getppid()}:
                    raise RuntimeError("Recorded worker identity refers to the runtime; recovery stopped")
                descendants = process.children(recursive=True)
                for child in reversed(descendants):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                process.kill()
                _, alive = psutil.wait_procs([process, *descendants], timeout=3)
                if any(p.is_running() and p.status() != psutil.STATUS_ZOMBIE for p in alive):
                    raise RuntimeError("An interrupted worker could not be stopped; recovery stopped")
                self.db.audit("orphan_worker_stopped", json.dumps({"worker": row["id"], "task_id": row["task_id"]}))
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                raise RuntimeError("Cannot inspect or stop an interrupted worker; recovery stopped") from None

    def _finish_log(self, task, proc, reason):
        log = self.logs.pop(task["worker"], None)
        if log:
            log.finish(f"\n[supervisor] exit_code={proc.returncode} reason={reason}\n")

    @staticmethod
    def _terminate(proc):
        # Kill descendants of bounded jobs, including commands launched by terminal.run.
        try:
            import psutil
            parent = psutil.Process(proc.pid)
            descendants = parent.children(recursive=True)
            for child in descendants:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
        except (ImportError, ProcessLookupError):
            pass
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    def _heartbeat(self, task, state):
        with self.store.transaction() as c:
            c.execute("UPDATE workers SET heartbeat=?,state=? WHERE id=?", (utc_now(), state, task["worker"]))

    def _log_path(self, task_id):
        path = self.db.path.parent / "logs" / "tasks" / f"{task_id}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def tick(self):
        for lane, (proc, task, started) in list(self.children.items()):
            current = self.store.get(task["id"])
            expired = time.monotonic() - started >= task["timeout"]
            if proc.poll() is not None or current["cancel_requested"] or expired:
                if proc.poll() is None:
                    self._terminate(proc)
                else:
                    proc.wait()
                ModelArbiter(self.db).release_task(task["id"])
                if current["state"] in ACTIVE and not (current["type"] == "workshop" and current["state"] == "waiting"):
                    self.store.interrupted(task["id"], "Cancelled" if current["cancel_requested"] else
                                           "Task deadline exceeded" if expired else f"Worker exited ({proc.returncode})")
                self._finish_log(task, proc, "cancelled" if current["cancel_requested"] else
                                 "deadline exceeded" if expired else "worker exit")
                self._heartbeat(task, "stopped")
                del self.children[lane]
            else:
                self._heartbeat(task, "running")
        reconcile_workshops(self.store)
        self.store.schedule_goal()
        # Reserved lanes keep journal/reflection progress independent from long executive jobs.
        for lane, kinds in (("conversation", ("conversation",)),
                            ("reflection", ("reflection",)), ("journal", ("journal",)),
                            ("work", ("executive", "capability", "tool_invocation")),
                            ("research", ("research",)), ("workshop", ("workshop", "workshop_stage", "curiosity"))):
            if lane in self.children or self.stop_event.is_set():
                continue
            task = self.store.claim(f"{lane}-{uuid.uuid4().hex[:8]}", kinds)
            if task:
                proc = None
                try:
                    # The private pipe is a launch gate, never interactive owner input.
                    # EOF before approval makes a newly orphaned worker exit unused.
                    log_path = self._log_path(task["id"])
                    with self.store.transaction() as c:
                        c.execute("UPDATE tasks SET worker_log=? WHERE id=?", (str(log_path), task["id"]))
                    proc = subprocess.Popen([sys.executable, "-B", "-u", "-m", "lilith.workers", str(self.db.path),
                                             str(task["id"]), self.workspace, self.model, "--managed"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    self.logs[task["worker"]] = WorkerLog(log_path, proc.stdout)
                    import psutil
                    created = psutil.Process(proc.pid).create_time()
                    with self.store.transaction() as c:
                        c.execute("UPDATE workers SET pid=?,process_created=? WHERE id=?",
                                  (proc.pid, created, task["worker"]))
                    proc.stdin.write(b"1")
                    proc.stdin.close()
                except Exception as error:
                    if proc is not None:
                        if proc.stdin and not proc.stdin.closed:
                            proc.stdin.close()
                        self._terminate(proc)
                        self._finish_log(task, proc, "launch failed")
                    self.store.interrupted(task["id"], str(error))
                    self._heartbeat(task, "stopped")
                    raise
                self.children[lane] = (proc, task, time.monotonic())

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        if self.lock:
            self.lock.close()


if __name__ == "__main__":
    if "--managed" not in sys.argv[5:] or sys.stdin.buffer.read(1) == b"1":
        execute_task(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4])
