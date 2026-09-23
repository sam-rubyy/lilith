"""One supervisor, bounded spawned workers, isolated connections, persistent heartbeats."""
from dataclasses import asdict
import json
import os
import subprocess
import sys
from pathlib import Path
import threading
import time
import uuid

from lilith.capabilities import CapabilityBroker
from lilith.database import Database, utc_now
from lilith.executive import Executive
from lilith.model_gateway import OllamaGateway
from lilith.reflection import ReflectionEngine
from lilith.self_state import SelfState
from lilith.tasks import ACTIVE, TaskStore
from lilith.research import Research, PublicHTTP
from lilith.tool_registry import ToolRegistry
from lilith.workshop import Workshop, start_workshop, reconcile_workshops


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
                response, reflection_id = Conversation(db, OllamaGateway(model)).reply(row["message"], token, check=check)
                with store.transaction() as c:
                    c.execute("UPDATE inbox SET response=?,state='completed',updated_at=? WHERE id=?", (response, utc_now(), request_id))
                result = {"request_id": request_id, "reflection_task_id": reflection_id}
            except Exception as error:
                with store.transaction() as c:
                    c.execute("UPDATE inbox SET response=?,state='needs_review',error=?,updated_at=? WHERE id=?",
                              ("".join(chunks), str(error), utc_now(), request_id))
                raise
        elif task["type"] == "curiosity":
            interests = db.get_interests(limit=8)
            proposal = OllamaGateway(model).chat_json([
                {"role": "system", "content": "Choose one small public research question you would like to explore during an idle curiosity window. "
                 "Base it on recorded interests; if none exist, choose a modest topic about software, nature, art, or science. "
                 "Avoid private owner details. Return topic, question (a short search query), and motivation (one sentence explaining the choice)."},
                {"role": "user", "content": json.dumps({"interests": interests})},
            ], schema={"type": "object", "properties": {key: {"type": "string"} for key in ("topic", "question", "motivation")},
                       "required": ["topic", "question", "motivation"], "additionalProperties": False})
            if any(not isinstance(proposal.get(key), str) or not 1 <= len(proposal[key]) <= limit
                   for key, limit in (("topic", 100), ("question", 500), ("motivation", 500))):
                raise ValueError("Invalid curiosity proposal")
            SelfState(db).add_interest(proposal["topic"], amount=0.02)
            research_id = store.enqueue("research", {"query": proposal["question"]}, priority=5,
                                        origin="curiosity", parent_task_id=task_id, timeout=240)
            db.journal(title="A question for my quiet time", body=proposal["motivation"] + "\n\n" + proposal["question"])
            result = {**proposal, "research_task_id": research_id}
        elif task["type"] == "reflection":
            store.transition(task_id, "reflecting")
            result = asdict(ReflectionEngine(db, SelfState(db), OllamaGateway(model)).reflect(**payload))
            store.enqueue("journal", {"title": f"Interaction reflection #{task_id}", "body": str(result)},
                          origin="reflection", priority=10, parent_task_id=task_id)
        elif task["type"] == "journal":
            db.journal(body=payload["body"], title=payload.get("title"))
            result = {"written": True}
        elif task["type"] == "executive":
            Executive(db, store, OllamaGateway(model), workspace).run(task)
            return
        elif task["type"] == "research":
            def check():
                if store.get(task_id)["cancel_requested"]:
                    raise RuntimeError("Research cancelled")
            result = Research(db, store, OllamaGateway(model), http=PublicHTTP(check=check)).run(task)
        elif task["type"] == "workshop":
            start_workshop(store, task)
            return
        elif task["type"] == "workshop_stage":
            result = Workshop(db, store, OllamaGateway(model), workspace).run_stage(task)
            if store.get(task_id)["state"] not in ACTIVE:
                return
        elif task["type"] == "tool_invocation":
            result = ToolRegistry(db, store).invoke(payload["name"], payload["data"], task_id=task_id)
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
        store.interrupted(task_id, f"{type(error).__name__}: {error}")
    finally:
        db.close()


class WorkerManager:
    def __init__(self, database, workspace, model, *, poll_interval=0.25):
        self.db = database
        self.store = TaskStore(database)
        self.workspace = str(Path(workspace).resolve())
        self.model = model
        self.interval = poll_interval
        self.stop_event = threading.Event()
        self.children = {}
        self.thread = None
        self.lock = None
        self.error = None

    def start(self):
        self.lock = RuntimeLock(self.db.path)
        try:
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
            self.error = str(error)
            self.db.audit("supervisor_failed", str(error), "worker_manager")
        finally:
            for lane, (proc, task, started) in list(self.children.items()):
                self._terminate(proc)
                self.store.interrupted(task["id"], "Runtime shutdown interrupted worker")
                self._heartbeat(task, "stopped")
                del self.children[lane]

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

    def tick(self):
        for lane, (proc, task, started) in list(self.children.items()):
            current = self.store.get(task["id"])
            expired = time.monotonic() - started >= task["timeout"]
            if proc.poll() is not None or current["cancel_requested"] or expired:
                if proc.poll() is None:
                    self._terminate(proc)
                else:
                    proc.wait()
                if current["state"] in ACTIVE and not (current["type"] == "workshop" and current["state"] == "waiting"):
                    self.store.interrupted(task["id"], "Cancelled" if current["cancel_requested"] else
                                           "Task deadline exceeded" if expired else f"Worker exited ({proc.returncode})")
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
                try:
                    # Explicit stdin isolation is essential for a worker launched from
                    # a Windows console or from a parent with piped interactive input.
                    proc = subprocess.Popen([sys.executable, "-B", "-m", "lilith.workers", str(self.db.path),
                                             str(task["id"]), self.workspace, self.model],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                except Exception as error:
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
    execute_task(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4])
