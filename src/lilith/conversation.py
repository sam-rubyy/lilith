"""Shared streaming conversation for the console and persistent service."""
import json
import time

from lilith.database import utc_now
from lilith.intent import needs_routing
from lilith.memory import MemoryService
from lilith.personality import SYSTEM_PROMPT
from lilith.self_state import SelfState
from lilith.tasks import TaskStore


def prompt_messages(database, self_state):
    store = TaskStore(database)
    recent = store.rows("SELECT id,type,state,result FROM tasks WHERE type NOT IN ('conversation','reflection','journal') ORDER BY id DESC LIMIT 5")
    activity = []
    for task in recent:
        result = json.loads(task["result"]) if task["result"] else None
        activity.append({"id": task["id"], "type": task["type"], "state": task["state"], "result": str(result)[:350] if result else None})
    memories = MemoryService(database).retrieve(limit=6)
    context = ("\n\nSELF AND INTERESTS\n" + self_state.prompt_context()[:1600]
               + "\nMEMORIES\n" + json.dumps(memories, ensure_ascii=False)[:1800]
               + "\nRECENT ACTIVITY\n" + json.dumps(activity, ensure_ascii=False)[:1200])
    history = database.recent_messages(limit=10)
    selected, remaining = [], 4500
    for message in reversed(history):
        content = message["content"][-min(2200, remaining):]
        selected.append({"role": message["role"], "content": content})
        remaining -= len(content)
        if remaining <= 0:
            break
    return [{"role": "system", "content": SYSTEM_PROMPT + context}, *reversed(selected)]


class Conversation:
    def __init__(self, database, gateway, router_gateway=None):
        self.db, self.gateway = database, gateway
        self.router_gateway = router_gateway or gateway
        self.self_state = SelfState(database)
        self.store = TaskStore(database)

    def reply(self, text, on_token, *, check=None):
        if not text.strip() or len(text) > 16000:
            raise ValueError("Messages must contain 1–16000 characters")
        started = time.monotonic()
        router_used = needs_routing(text)
        router_duration = 0.0
        first_token = None
        succeeded = False
        owner_message_id = self.db.save_message("user", text)
        self.self_state.on_interaction_started()
        chunks, size = [], 0
        try:
            from lilith.intent import route_request
            routed = None
            if router_used:
                router_started = time.monotonic()
                try:
                    routed = route_request(self.db, self.store, self.router_gateway, text, check)
                finally:
                    router_duration = time.monotonic() - router_started
            stream = [routed] if routed else self.gateway.chat_stream(prompt_messages(self.db, self.self_state))
            for chunk in stream:
                if check:
                    check()
                chunks.append(chunk)
                size += len(chunk)
                if size > 65536:
                    raise RuntimeError("Reply text exceeded its budget")
                if first_token is None:
                    first_token = time.monotonic() - started
                on_token(chunk)
            response = "".join(chunks)
            if not response.strip():
                raise RuntimeError("Model returned an empty reply")
        except Exception as error:
            self.self_state.on_interaction_failed()
            self.db.audit("conversation_failed", str(error), "conversation")
            self._record_metrics(None, started, router_used, router_duration, first_token, False)
            raise
        self.self_state.on_interaction_succeeded()
        succeeded = True
        lilith_message_id = self.db.save_message("assistant", response, model=self.gateway.model_name)
        self._record_metrics(None, started, router_used, router_duration, first_token, succeeded)
        task_id = self.store.enqueue("reflection", {"user_message": text, "assistant_response": response,
                                     "owner_message_id": owner_message_id,
                                     "lilith_message_id": lilith_message_id},
                                     priority=30, origin="conversation", timeout=360)
        return response, task_id

    def _record_metrics(self, task_id, started, router_used, router_duration, first_token, succeeded):
        with self.db.lock:
            cursor = self.db.connection.execute(
                """INSERT INTO conversation_metrics(
                    task_id,created_at,router_used,router_duration,time_to_first_token,
                    total_conversation_duration,succeeded) VALUES (?,?,?,?,?,?,?)""",
                (task_id, utc_now(), int(router_used), router_duration, first_token,
                 time.monotonic() - started, int(succeeded)),
            )
            self.db.connection.commit()
            return int(cursor.lastrowid)
