"""Shared streaming conversation for the console and persistent service."""
import json

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
    memories = database.recent_memories(limit=6)
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
    def __init__(self, database, gateway):
        self.db, self.gateway = database, gateway
        self.self_state = SelfState(database)
        self.store = TaskStore(database)

    def reply(self, text, on_token, *, check=None):
        if not text.strip() or len(text) > 16000:
            raise ValueError("Messages must contain 1–16000 characters")
        self.db.save_message("user", text)
        self.self_state.on_interaction_started()
        chunks, size = [], 0
        try:
            from lilith.intent import route_request
            routed = route_request(self.db, self.store, self.gateway, text, check)
            stream = [routed] if routed else self.gateway.chat_stream(prompt_messages(self.db, self.self_state))
            for chunk in stream:
                if check:
                    check()
                chunks.append(chunk)
                size += len(chunk)
                if size > 65536:
                    raise RuntimeError("Reply text exceeded its budget")
                on_token(chunk)
            response = "".join(chunks)
            if not response.strip():
                raise RuntimeError("Model returned an empty reply")
        except Exception as error:
            self.self_state.on_interaction_failed()
            self.db.audit("conversation_failed", str(error), "conversation")
            raise
        self.self_state.on_interaction_succeeded()
        self.db.save_message("assistant", response, model=self.gateway.model_name)
        task_id = self.store.enqueue("reflection", {"user_message": text, "assistant_response": response},
                                     priority=30, origin="conversation", timeout=360)
        return response, task_id
