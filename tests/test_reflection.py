import tempfile
import unittest
from pathlib import Path

from lilith.database import Database
from lilith.reflection import REFLECTION_SCHEMA, ReflectionEngine
from lilith.self_state import SelfState


class FakeGateway:
    def __init__(self, proposal):
        self.proposal = proposal

    def chat_json(self, messages, schema=None):
        self.messages = messages
        self.schema = schema
        return self.proposal


class ReflectionProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "state.db")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_owner_and_lilith_are_separate_and_schema_is_used(self):
        gateway = FakeGateway({"memories": [], "self_beliefs": [], "interests": [], "affect": []})
        ReflectionEngine(self.db, SelfState(self.db), gateway).reflect(
            "I like robotics.", "Maybe we should make a game together.")
        prompt = gateway.messages[1]["content"]
        self.assertIn("\n\nOWNER MESSAGE\nI like robotics.\n\nLILITH RESPONSE\n", prompt)
        self.assertIs(gateway.schema, REFLECTION_SCHEMA)

    def test_lilith_response_cannot_become_owner_memory(self):
        proposal = {"memories": [{"type": "project", "content": "The owner proposed making a game.",
                                   "evidence_source": "lilith_response", "confidence": 0.9,
                                   "importance": 0.8}],
                    "self_beliefs": [], "interests": [], "affect": []}
        owner = self.db.save_message("user", "I like robotics.")
        outcome = ReflectionEngine(self.db, SelfState(self.db), FakeGateway(proposal)).reflect(
            "I like robotics.", "Maybe we should make a game together.", owner)
        self.assertEqual(outcome.memories_created, 0)
        self.assertEqual(self.db.recent_memories(), [])

    def test_owner_evidence_can_be_saved(self):
        proposal = {"memories": [{"type": "preference", "content": "I like robotics.",
                                   "evidence_source": "owner_message", "confidence": 0.95,
                                   "importance": 0.7}],
                    "self_beliefs": [], "interests": [], "affect": []}
        owner = self.db.save_message("user", "I like robotics.")
        assistant = self.db.save_message("assistant", "Maybe we should make a game together.")
        outcome = ReflectionEngine(self.db, SelfState(self.db), FakeGateway(proposal)).reflect(
            "I like robotics.", "Maybe we should make a game together.", owner, assistant)
        self.assertEqual(outcome.memories_created, 1)
        memory = self.db.recent_memories()[0]
        self.assertEqual(memory["content"], "I like robotics.")
        self.assertEqual(memory["owner_message_id"], owner)
        self.assertEqual(memory["lilith_message_id"], assistant)

    def test_model_cannot_relabel_its_own_words_as_owner_evidence(self):
        owner_text = "I like robotics."
        response = "Maybe we should make a game together. *smiles*"
        owner = self.db.save_message("user", owner_text)
        assistant = self.db.save_message("assistant", response)
        for content in (response, "The owner proposed making a game.", "*smiles*"):
            with self.subTest(content=content):
                proposal = {"memories": [{"type": "project", "content": content,
                            "evidence_source": "owner_message", "confidence": 1, "importance": 1}]}
                outcome = ReflectionEngine(self.db, SelfState(self.db), FakeGateway(proposal)).reflect(
                    owner_text, response, owner, assistant)
                self.assertEqual(outcome.memories_created, 0)
        self.assertEqual(self.db.recent_memories(), [])

    def test_missing_or_mismatched_saved_evidence_fails_closed(self):
        proposal = {"memories": [{"type": "preference", "content": "I like robotics.",
                    "evidence_source": "owner_message"}]}
        engine = ReflectionEngine(self.db, SelfState(self.db), FakeGateway(proposal))
        self.assertEqual(engine.reflect("I like robotics.", "Hello").memories_created, 0)
        assistant = self.db.save_message("assistant", "I like robotics.")
        with self.assertRaisesRegex(ValueError, "saved evidence"):
            engine.reflect("I like robotics.", "Hello", assistant)
        owner = self.db.save_message("user", "I dislike robotics.")
        with self.assertRaisesRegex(ValueError, "saved evidence"):
            engine.reflect("I like robotics.", "Hello", owner)
        self.assertEqual(self.db.recent_memories(), [])

    def test_nonfinite_affect_and_interest_updates_are_ignored(self):
        state = SelfState(self.db)
        before = state.affect()
        for value in (float("nan"), float("inf"), float("-inf")):
            proposal = {"affect": [{"name": "curiosity", "delta": value}],
                        "interests": [{"topic": "robotics", "delta": value}]}
            outcome = ReflectionEngine(self.db, state, FakeGateway(proposal)).reflect("Hi", "Hello")
            self.assertEqual(outcome.affect_changed, 0)
            self.assertEqual(outcome.interests_changed, 0)
        self.assertEqual(state.affect(), before)
        self.assertEqual(self.db.get_interests(), [])

    def test_save_message_returns_ids_and_reflection_task_retains_them(self):
        from lilith.conversation import Conversation

        class ConversationGateway:
            model_name = "fake"
            def chat_stream(self, messages):
                yield "Hello."

        response, task_id = Conversation(self.db, ConversationGateway()).reply("Hi", lambda _: None)
        task = __import__("lilith.tasks", fromlist=["TaskStore"]).TaskStore(self.db).get(task_id)
        self.assertEqual(response, "Hello.")
        self.assertIsInstance(task["input"]["owner_message_id"], int)
        self.assertEqual(task["input"]["lilith_message_id"], task["input"]["owner_message_id"] + 1)


if __name__ == "__main__":
    unittest.main()
