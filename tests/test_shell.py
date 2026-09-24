"""Owner-granted shell tests use only disposable files and deterministic fixtures."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lilith.capabilities import CapabilityBroker, CapabilityError
from lilith.database import Database
from lilith.intent import route_request
from lilith.tasks import TaskStore
from lilith.tool_registry import ToolRegistry
from lilith.workers import execute_task
from lilith.workshop import start_workshop, reconcile_workshops, Workshop


COMMAND = "printf hit >> ../hits.txt"
DESIGN = {"name": "shell-probe", "purpose": "Test a granted command",
          "inputs": {"type": "object", "properties": {}, "additionalProperties": False},
          "outputs": {"type": "string"}, "permissions": ["shell"]}
SOURCE = """def run(data):
    result = shell('printf hit >> ../hits.txt')
    if get(result, 'returncode') != 0:
        fail('Command failed')
    return get(result, 'output')
"""
IMPLEMENTATION = {"source": SOURCE, "readme": "An offline test fixture.", "tests": [
    {"input": {}, "expected": "", "shell_calls": [{"command": COMMAND, "result": {"returncode": 0, "output": ""}}]},
    {"input": {}, "expected": "hello", "shell_calls": [{"command": COMMAND, "result": {"returncode": 0, "output": "hello"}}]},
    {"input": {}, "expect_error": True, "shell_calls": [{"command": COMMAND, "result": {"returncode": 1, "output": "failed"}}]},
]}


class Gateway:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.prompts = []

    def chat_json(self, messages, **kwargs):
        self.prompts.append(messages)
        return copy.deepcopy(next(self.replies))


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.db = Database(self.root / "state.db")
        self.addCleanup(self.db.close)
        self.store = TaskStore(self.db)
        self.registry = ToolRegistry(self.db, self.store)
        self.env = patch.dict(os.environ, {"LILITH_ALLOW_SHELL": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_shell_requires_runtime_grant_even_if_model_requests_it(self):
        with patch.dict(os.environ, {"LILITH_ALLOW_SHELL": "0"}):
            with self.assertRaisesRegex(CapabilityError, "disabled"):
                CapabilityBroker(self.db, self.workspace, allowed={"shell.run"}).invoke("shell.run", {"command": COMMAND})
            with self.assertRaisesRegex(ValueError, "grants"):
                self.registry.create(1, DESIGN, IMPLEMENTATION)
            proposal = {"action": "shell", "request": "test", "data": {"command": COMMAND}}
            with self.assertRaises(ValueError):
                route_request(self.db, self.store, Gateway(proposal), "execute this")
        self.assertFalse((self.root / "hits.txt").exists())

    @unittest.skipIf(os.name == "nt", "POSIX shell syntax")
    def test_shell_has_full_filesystem_permissions_and_returns_output(self):
        result = CapabilityBroker(self.db, self.workspace, allowed={"shell.run"}).invoke(
            "shell.run", {"command": COMMAND + "; printf done"})
        self.assertEqual(result, {"returncode": 0, "output": "done"})
        # The effect is outside the workspace, but still inside the test directory.
        self.assertEqual((self.root / "hits.txt").read_text(), "hit")

    def test_conversation_queues_shell_without_retry(self):
        proposal = {"action": "shell", "request": "Print test output", "data": {"command": "echo test"}}
        reply = route_request(self.db, self.store, Gateway(proposal), "print test using the shell")
        task = self.store.get(1)
        self.assertIn("Shell task #1", reply)
        self.assertEqual(task["input"]["capability"], "shell.run")
        self.assertFalse(task["resumable"])

    @unittest.skipIf(os.name == "nt", "POSIX shell syntax")
    def test_executive_can_use_owner_granted_shell(self):
        from lilith.executive import Executive
        ident = self.store.enqueue("executive", {"request": "Run the disposable test command"})
        task = self.store.claim("executive", ["executive"])
        step = {"capability": "shell.run", "arguments": {"command": COMMAND}, "expected": "append marker"}
        model = Gateway({"steps": [step], "needs_review": False}, {"approved": True},
                        {"completed": True, "summary": "Command returned zero"})
        Executive(self.db, self.store, model, self.workspace).run(task)
        self.assertEqual(self.store.get(ident)["state"], "completed")
        self.assertEqual((self.root / "hits.txt").read_text(), "hit")

    def test_generated_tests_mock_commands_and_revocation_is_enforced(self):
        name = self.registry.create(1, DESIGN, IMPLEMENTATION)["name"]
        with patch("lilith.capabilities.subprocess.Popen") as launch:
            report = self.registry.test(name, task_id=2)
            launch.assert_not_called()
        self.assertTrue(report["passed"])
        self.assertTrue(report["shell_calls_mocked"])
        self.registry.status(name, "experimental", {**report, "review_approved": True})
        with self.assertRaisesRegex(ValueError, "grant"):
            self.registry.invoke(name, {}, task_id=3, workspace=self.workspace)
        self.assertFalse(self.registry.list(usable=True, read_only=True))
        with patch.dict(os.environ, {"LILITH_ALLOW_SHELL": "0"}):
            self.assertFalse(self.registry.list(usable=True))
            with self.assertRaisesRegex(ValueError, "grant"):
                self.registry.invoke(name, {}, task_id=3, allow_shell=True, workspace=self.workspace)
        self.assertFalse((self.root / "hits.txt").exists())

    def test_mock_mismatch_is_not_an_expected_tool_error(self):
        implementation = copy.deepcopy(IMPLEMENTATION)
        implementation["tests"][-1]["shell_calls"] = []
        name = self.registry.create(1, DESIGN, implementation)["name"]
        self.assertFalse(self.registry.test(name, task_id=2)["passed"])
        self.assertEqual(self.registry.list()[0]["status"], "disabled")

    def stage(self, parent, proposal=None):
        child = self.store.claim("stage", ["workshop_stage"])
        self.assertEqual(child["id"], self.store.get(parent)["result"]["child_task_id"])
        with patch("lilith.workers.gateway", return_value=Gateway(proposal)):
            execute_task(self.db.path, child["id"], self.workspace, "fake")
        self.assertEqual(self.store.get(child["id"])["state"], "completed", self.store.get(child["id"]))
        reconcile_workshops(self.store)

    @unittest.skipIf(os.name == "nt", "POSIX shell syntax")
    def test_shell_workshop_skips_research_and_executes_real_command_once(self):
        ident = self.store.enqueue("workshop", {"request": "Build a reusable shell tool", "data": {}})
        start_workshop(self.store, self.store.claim("workshop", ["workshop"]))
        self.stage(ident, {"route": "build", "research_query": ""})
        self.assertEqual(self.store.get(ident)["result"]["stage"], "design")
        self.stage(ident, DESIGN)
        self.stage(ident, IMPLEMENTATION)
        self.stage(ident)
        self.stage(ident, {"approved": True})
        self.stage(ident)  # Canary validates the contract, without host effects.
        self.assertFalse((self.root / "hits.txt").exists())
        self.stage(ident)  # One actual execution.
        self.stage(ident, {"verified": True})
        self.assertEqual(self.store.get(ident)["state"], "completed")
        self.assertEqual((self.root / "hits.txt").read_text(), "hit")
        self.assertFalse(self.store.rows("SELECT id FROM tasks WHERE type='research'"))

    def test_failing_generated_test_is_corrected_before_artifact_creation(self):
        ident = self.store.enqueue("workshop", {"request": "Build a shell tool", "data": {}})
        start_workshop(self.store, self.store.claim("workshop", ["workshop"]))
        self.stage(ident, {"route": "build", "research_query": ""})
        self.stage(ident, DESIGN)
        child = self.store.claim("implementation", ["workshop_stage"])
        broken = {**IMPLEMENTATION, "source": "def run(data): return 'wrong'"}
        gateway = Gateway(broken, IMPLEMENTATION)
        Workshop(self.db, self.store, gateway, self.workspace).run_stage(child)
        self.assertEqual(len(gateway.prompts), 2)
        self.assertEqual(len(self.registry.list()), 1)
        self.assertFalse((self.root / "hits.txt").exists())
