import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lilith.conversation import Conversation
from lilith.database import Database, utc_now
from lilith.home import LilithHome
from lilith.model_gateway import OllamaGateway
from lilith.service import RuntimeState


class StreamingTests(unittest.TestCase):
    def test_ndjson_deltas_before_completion_and_no_thinking(self):
        stream = io.BytesIO(b'{"message":{"thinking":"private reasoning","content":"Hello"},"done":false}\n'
                            b'{"message":{"content":" there"},"done":false}\n{"done":true}\n')
        with patch("urllib.request.urlopen", return_value=stream) as request:
            result = OllamaGateway("fake").chat_stream([])
            self.assertEqual(next(result), "Hello")
            self.assertEqual(next(result), " there")
            with self.assertRaises(StopIteration):
                next(result)
        payload = json.loads(request.call_args.args[0].data)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["keep_alive"], "0")

    def test_incomplete_stream_raises_after_partial_text(self):
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"message":{"content":"partial"}}\n')):
            result = OllamaGateway("fake").chat_stream([])
            self.assertEqual(next(result), "partial")
            with self.assertRaisesRegex(RuntimeError, "before completion"):
                next(result)

    def test_streamed_error_is_not_silently_complete(self):
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"error":"model unavailable"}\n')):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                list(OllamaGateway("fake").chat_stream([]))


class RuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "state.db")
        self.runtime = RuntimeState(self.db)
        self.store = self.runtime.store

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_inbox_dispatch_is_idempotent(self):
        ident = self.runtime.submit("Hello")
        self.runtime.dispatch()
        self.runtime.dispatch()
        self.assertEqual(len(self.store.rows("SELECT * FROM tasks WHERE type='conversation'")), 1)
        row = self.store.rows("SELECT * FROM inbox WHERE id=?", (ident,))[0]
        self.assertIsNotNone(row["task_id"])

    def test_curiosity_idle_interval_daily_budget_and_pause(self):
        now = datetime.now(timezone.utc)
        with self.store.transaction() as c:
            c.execute("INSERT INTO runtime_status VALUES (1,1,?,?,'idle',0,'fake')", ((now - timedelta(hours=2)).timestamp(), utc_now()))
        ident = self.runtime.schedule_curiosity(now)
        self.assertIsNotNone(ident)
        task = self.store.claim("curiosity", ["curiosity"])
        self.store.transition(task["id"], "completed", result={})
        self.assertIsNone(self.runtime.schedule_curiosity(now + timedelta(minutes=30)))
        self.runtime.set("daily_sessions", 1)
        self.assertIsNone(self.runtime.schedule_curiosity(now + timedelta(hours=2)))
        self.runtime.set("curiosity_enabled", False)
        self.assertIsNone(self.runtime.schedule_curiosity(now + timedelta(days=1)))

    def test_owner_message_interrupts_curiosity_and_prevents_new_work(self):
        task = self.store.enqueue("research", {"query": "public question"}, origin="curiosity")
        self.runtime.submit("I'm here")
        self.assertEqual(self.store.get(task)["state"], "cancelled")
        self.assertIsNone(self.runtime.schedule_curiosity(datetime.now(timezone.utc) + timedelta(days=1)))

    def test_first_boot_stays_model_idle(self):
        with self.store.transaction() as c:
            c.execute("INSERT INTO runtime_status VALUES (1,1,?,?,'idle',0,'fake')", (datetime.now(timezone.utc).timestamp(), utc_now()))
        self.assertIsNone(self.runtime.schedule_curiosity())
        self.assertFalse(self.store.rows("SELECT * FROM tasks"))

    def test_curiosity_records_intention_and_queues_research(self):
        from lilith.workers import execute_task
        ident = self.store.enqueue("curiosity", {}, origin="self_directed")
        self.store.claim("curiosity", ["curiosity"])
        with patch("lilith.workers.OllamaGateway") as gateway:
            gateway.return_value.chat_json.return_value = {
                "topic": "Botany", "question": "How do plants sense light?",
                "motivation": "I want to understand how a plant finds the sun."}
            execute_task(self.db.path, ident, self.temp.name, "fake")
        result = self.store.get(ident)
        self.assertEqual(result["state"], "completed")
        research = self.store.get(result["result"]["research_task_id"])
        self.assertEqual(research["type"], "research")
        self.assertEqual(research["origin"], "curiosity")
        self.assertTrue(self.store.rows("SELECT id FROM journal_entries WHERE title='A question for my quiet time'"))

    def test_restart_marks_partial_reply_for_review(self):
        self.runtime.submit("Hello")
        self.runtime.dispatch()
        self.store.claim("conversation", ["conversation"])
        with self.store.transaction() as c:
            c.execute("UPDATE inbox SET response='partial answer',state='streaming'")
        self.store.recover()
        self.runtime.sync_failures()
        row = self.store.rows("SELECT * FROM inbox")[0]
        self.assertEqual(row["state"], "needs_review")
        self.assertEqual(row["response"], "partial answer")

    def test_conversation_records_complete_response_and_reflection(self):
        class Gateway:
            model_name = "fake"
            def chat_stream(self, messages):
                self.messages = messages
                yield "Hey. "
                yield "Good to see you."
        gateway = Gateway()
        tokens = []
        response, ident = Conversation(self.db, gateway).reply("hi", tokens.append)
        self.assertEqual(tokens, ["Hey. ", "Good to see you."])
        self.assertEqual(self.db.recent_messages()[-1]["content"], response)
        self.assertEqual(self.store.get(ident)["type"], "reflection")
        self.assertIn("owner retains authority", gateway.messages[0]["content"])

    def test_memory_and_interest_commands_available_to_dashboard(self):
        from lilith.commands import handle_command
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(handle_command('/remember I like quiet mornings', self.store, None))
            self.assertTrue(handle_command('/interest botany', self.store, None))
            self.assertTrue(handle_command('/affect curiosity 0.7', self.store, None))
        self.assertEqual(self.db.recent_memories()[0]['content'], 'I like quiet mornings')
        self.assertEqual(self.db.get_interests()[0]['topic'], 'botany')
        self.assertEqual(self.db.get_affect('curiosity'), 0.7)


class HomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_send_stream_monitor_and_pause(self):
        with tempfile.TemporaryDirectory() as temp:
            app = LilithHome(Path(temp) / "state.db", start_service=False)
            async with app.run_test(size=(140, 44)) as pilot:
                from textual.widgets import Input, DataTable, Static
                app.query_one("#message", Input).value = "hello from home"
                await pilot.press("enter")
                await pilot.pause()
                rows = app.store.rows("SELECT * FROM inbox")
                self.assertEqual(rows[0]["message"], "hello from home")
                app.state.dispatch()
                with app.store.transaction() as c:
                    c.execute("UPDATE inbox SET response='Hello, ',state='streaming'")
                await app.refresh_views()
                self.assertIn("Hello,", str(app.message_widgets[(rows[0]["id"], "lilith")].content))
                self.assertGreater(app.query_one("#tasks", DataTable).row_count, 0)
                from textual.widgets import TabbedContent
                app.query_one(TabbedContent).active = "tasks-tab"
                await pilot.pause()
                table = app.query_one("#tasks", DataTable)
                table.focus()
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsNotNone(app.selected_task)
                await pilot.click("#cancel-task")
                self.assertEqual(app.store.get(app.selected_task)["state"], "cancelled")
                await pilot.click("#curiosity")
                self.assertFalse(app.state.settings()["curiosity_enabled"])
                await pilot.resize_terminal(85, 32)
                await pilot.pause()
                self.assertTrue(app.screen.has_class("narrow"))
                await pilot.press("ctrl+q")


if __name__ == "__main__":
    unittest.main()
