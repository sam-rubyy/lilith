import json
from pathlib import Path
import tempfile
import unittest

from lilith.activity import activity_text, work_progress
from lilith.conversation import Conversation
from lilith.database import Database
from lilith.tasks import TaskStore
from lilith.workshop import start_workshop, reconcile_workshops


class Gateway:
    model_name = "fake"

    def __init__(self, action="build", **fields):
        self.proposal = dict(action=action, request="Create a journal companion using sample input",
                             data={"entry": "Today I learned about stars"}, tool="", question="", **{})
        self.proposal.update(fields)
        self.streamed = False

    def chat_json(self, messages, **kwargs):
        self.messages = messages
        return self.proposal

    def chat_stream(self, messages):
        self.streamed = True
        yield "Let’s talk."


class IntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "state.db")
        self.store = TaskStore(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_contextual_build_is_durable_and_chat_can_continue(self):
        self.db.save_message("assistant", "Let’s create a journal companion.")
        gateway = Gateway()
        response, _ = Conversation(self.db, gateway).reply("no, actually make it", lambda _: None)
        tasks = self.store.rows("SELECT id FROM tasks WHERE type='workshop'")
        self.assertEqual(len(tasks), 1)
        self.assertIn("journal companion", gateway.messages[1]["content"])
        self.assertFalse(gateway.streamed)
        task = self.store.claim("workshop", ["workshop"])
        start_workshop(self.store, task)
        reply, _ = Conversation(self.db, Gateway("chat")).reply("how are you?", lambda _: None)
        self.assertEqual(reply, "Let’s talk.")
        self.assertEqual(self.store.get(task["id"])["state"], "waiting")
        self.assertIn(f"#{task['id']}", response)

    def test_missing_input_asks_question_without_creating_work(self):
        response, _ = Conversation(self.db, Gateway("clarify", question="Which text should I process?")).reply("run it", lambda _: None)
        self.assertEqual(response, "Which text should I process?")
        self.assertFalse(self.store.rows("SELECT id FROM tasks WHERE type='workshop'"))

    def test_invalid_route_or_data_never_queues_work(self):
        for gateway in [Gateway(request=""), Gateway("run", tool="missing")]:
            with self.assertRaises(ValueError):
                Conversation(self.db, gateway).reply("make it", lambda _: None)
        self.assertFalse(self.store.rows("SELECT id FROM tasks"))

    def test_cancel_during_routing_does_not_create_work(self):
        def check():
            raise RuntimeError("cancelled")
        with self.assertRaises(RuntimeError):
            Conversation(self.db, Gateway()).reply("build it", lambda _: None, check=check)
        self.assertFalse(self.store.rows("SELECT id FROM tasks"))

    def test_obvious_conversation_bypasses_router_and_records_latency(self):
        gateway = Gateway("build")
        response, _ = Conversation(self.db, gateway).reply("how are you?", lambda _: None)
        self.assertEqual(response, "Let’s talk.")
        self.assertFalse(hasattr(gateway, "messages"))
        metric = self.store.rows("SELECT * FROM conversation_metrics")[0]
        self.assertEqual(metric["router_used"], 0)
        self.assertEqual(metric["router_duration"], 0.0)
        self.assertIsNotNone(metric["time_to_first_token"])
        self.assertGreaterEqual(metric["total_conversation_duration"], metric["time_to_first_token"])

    def test_uncertain_message_still_uses_router(self):
        gateway = Gateway("chat")
        Conversation(self.db, gateway).reply("Could we revisit the parser idea?", lambda _: None)
        self.assertTrue(hasattr(gateway, "messages"))
        self.assertEqual(self.store.rows(
            "SELECT router_used FROM conversation_metrics"
        )[0]["router_used"], 1)

    def test_progress_tracks_actual_completed_stages_and_failure(self):
        ident = self.store.enqueue("workshop", {"request": "normalize tags", "data": {}})
        task = self.store.claim("parent", ["workshop"])
        start_workshop(self.store, task)
        self.assertEqual(work_progress(self.store, self.store.get(ident))[0], 0)
        child = self.store.claim("child", ["workshop_stage"])
        self.store.transition(child["id"], "completed", result={"route": "build", "research_query": "Python strings"})
        reconcile_workshops(self.store)
        self.assertEqual(work_progress(self.store, self.store.get(ident))[0], 11)
        child = self.store.claim("research", ["research"])
        self.store.transition(child["id"], "needs_review", error="Search unavailable")
        reconcile_workshops(self.store)
        current = self.store.get(ident)
        self.assertLess(work_progress(self.store, current)[0], 100)
        self.assertIn("Search unavailable", activity_text(self.store, current))


if __name__ == "__main__":
    unittest.main()
