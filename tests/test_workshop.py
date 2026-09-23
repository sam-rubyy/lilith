import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lilith.database import Database
from lilith.tasks import TaskStore
from lilith.workers import execute_task
from lilith.tool_registry import ToolRegistry, check_schema, validate_value
from lilith.tool_sandbox import Sandbox, SandboxError, formatted_source
from lilith.workshop import start_workshop, reconcile_workshops
from lilith.workshop import Workshop

DESIGN = {"name": "normalize-tags", "purpose": "Normalize a list of text tags",
          "inputs": {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
                     "required": ["tags"], "additionalProperties": False},
          "outputs": {"type": "array", "items": {"type": "string"}}, "permissions": []}
SOURCE = """def run(data):
    result = []
    for tag in data['tags']:
        text = lower(strip(tag))
        if text not in result:
            result = result + [text]
    return sorted(result)
"""
IMPLEMENTATION = {"source": SOURCE, "readme": "# Tag normalizer\nLowercase, strip, deduplicate, and sort tags.", "tests": [
    {"input": {"tags": [" B ", "a", "A"]}, "expected": ["a", "b"]},
    {"input": {"tags": []}, "expected": []},
    {"input": {"tags": [1]}, "expect_error": True}]}


class FakeGateway:
    def __init__(self, *replies):
        self.replies = iter(replies)

    def chat_json(self, messages, **kwargs):
        return next(self.replies)


class SandboxTests(unittest.TestCase):
    def test_pure_transformation(self):
        data = {"tags": [" X ", "y", "X"]}
        self.assertEqual(Sandbox().run(SOURCE, data), ["x", "y"])
        self.assertEqual(data, {"tags": [" X ", "y", "X"]})

    def test_host_escape_syntax_is_rejected(self):
        for source in (
            "import os\ndef run(data): return os.environ",
            "def run(data): return open('secret').read()",
            "def run(data): return data.__class__",
            "def run(data): return eval(data)",
            "def run(data): return __import__('os')",
            "def run(data): return run(data)",
            "def run(data):\n while True: pass",
            "def run(data): return 2 ** 100000000",
            "def run(data): return [x for x in data]",
            "def run(data):\n data['x'] = 1\n return data",
            "@evil\ndef run(data): return data",
        ):
            with self.subTest(source=source), self.assertRaises(SandboxError):
                Sandbox().run(source, {})

    def test_operation_budget(self):
        with self.assertRaisesRegex(SandboxError, "budget"):
            Sandbox(operations=10).run("def run(data):\n for i in range(100):\n  x = i + 1\n return x", {})

    def test_time_budget(self):
        with patch("lilith.tool_sandbox.time.monotonic", side_effect=[0, 3]), \
             self.assertRaisesRegex(SandboxError, "budget"):
            Sandbox(seconds=2).run("def run(data):\n return data", {})

    def test_memory_amplification_blocked(self):
        for expression in ("'x' * 100000000", "range(100000000)", "replace('x' * 1000, 'x', 'y' * 1000)"):
            with self.subTest(expression=expression), self.assertRaises(SandboxError):
                Sandbox().run("def run(data): return " + expression, {})

    def test_invalid_json_and_return_type(self):
        with self.assertRaises(SandboxError):
            Sandbox().run("def run(data): return {1: 'bad'}", {})
        with self.assertRaises(SandboxError):
            Sandbox().run("def run(data): return float('nan')", {})
        with self.assertRaises(SandboxError):
            Sandbox().run("def run(data): return data", {"secret": object()})

    def test_helpers_and_conditionals(self):
        source = "def run(data):\n value = get(data, 'x', 2)\n return {'answer': value * 3 if value > 0 else 0, 'parts': split('a,b', ',')}"
        self.assertEqual(Sandbox().run(source, {}), {"answer": 6, "parts": ["a", "b"]})
        self.assertEqual(formatted_source(formatted_source(source)), formatted_source(source))

    def test_schema_subset_no_remote_refs(self):
        with self.assertRaises(ValueError):
            check_schema({"type": "object", "$ref": "https://example.com/schema"})
        with self.assertRaises(ValueError):
            check_schema({"type": "string", "minLength": 5, "maxLength": 2})
        with self.assertRaises(ValueError):
            check_schema({"type": "object", "properties": {1: {"type": "string"}}})
        with self.assertRaises(SandboxError):
            validate_value(True, {"type": "integer"})
        with self.assertRaises(SandboxError):
            validate_value({"unknown": 1}, DESIGN["inputs"])

    def test_unknown_names_rejected_before_running(self):
        with self.assertRaisesRegex(SandboxError, "Unknown name"):
            formatted_source("def run(data): return missing_value")


class WorkshopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "state.db")
        self.store = TaskStore(self.db)
        self.registry = ToolRegistry(self.db, self.store)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def make_tool(self, implementation=None):
        return self.registry.create(1, DESIGN, implementation or IMPLEMENTATION)["name"]

    def approve(self, name):
        report = self.registry.test(name, task_id=2)
        self.registry.status(name, "experimental", {**report, "review_approved": True})

    def test_registry_lifecycle_and_retained_invocation(self):
        name = self.make_tool()
        with self.assertRaises(ValueError):
            self.registry.invoke(name, {"tags": []}, task_id=2)
        self.approve(name)
        second = Database(self.db.path)
        try:
            registry = ToolRegistry(second, TaskStore(second))
            self.assertEqual(registry.invoke(name, {"tags": [" Y", "x"]}, task_id=3), ["x", "y"])
        finally:
            second.close()
        self.registry.status(name, "disabled")
        with self.assertRaises(ValueError):
            self.registry.invoke(name, {"tags": []}, task_id=4)

    def test_promotion_requires_test_and_review_evidence(self):
        name = self.make_tool()
        with self.assertRaises(ValueError):
            self.registry.status(name, "approved")
        self.registry.test(name, task_id=2)
        with self.assertRaises(ValueError):
            self.registry.status(name, "experimental")

    def test_tampered_source_is_never_executed(self):
        name = self.make_tool()
        self.approve(name)
        (self.registry.root / name / "tool.py").write_text("def run(data): return 'tampered'", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.registry.invoke(name, {"tags": []}, task_id=3)

    def test_manifest_permissions_and_failure_tests_required(self):
        with self.assertRaises(ValueError):
            self.registry.create(1, {**DESIGN, "permissions": ["terminal.run"]}, IMPLEMENTATION)
        with self.assertRaises(ValueError):
            self.registry.create(1, DESIGN, {**IMPLEMENTATION, "tests": [{"input": {"tags": []}, "expected": []}] * 3})

    def test_failed_generated_tests_disable_tool(self):
        name = self.make_tool({**IMPLEMENTATION, "source": "def run(data): return []"})
        report = self.registry.test(name, task_id=2)
        self.assertFalse(report["passed"])
        self.assertEqual(self.registry.list()[0]["status"], "disabled")

    def start(self, expected=None):
        payload = {"request": "Normalize the tags", "data": {"tags": [" B ", "a", "A"]}, "urls": ["https://example.com/docs"]}
        if expected is not None:
            payload["expected"] = expected
        ident = self.store.enqueue("workshop", payload)
        task = self.store.claim("workshop", ["workshop"])
        start_workshop(self.store, task)
        return ident

    def stage(self, parent_id, reply=None, research=False):
        parent = self.store.get(parent_id)
        child_id = parent["result"]["child_task_id"]
        child = self.store.claim("stage", ["research" if research else "workshop_stage"])
        self.assertEqual(child["id"], child_id)
        if research:
            self.store.transition(child_id, "completed", result={"claims": [{"text": "Normalize text", "source_ids": [1]}], "citations": []})
        else:
            with patch("lilith.workers.gateway", return_value=FakeGateway(reply)):
                execute_task(str(self.db.path), child_id, str(self.root), "fake")
        reconcile_workshops(self.store)
        return self.store.get(child_id)

    def build_to_canary(self, expected=None):
        ident = self.start(expected)
        self.stage(ident, {"route": "build", "research_query": "Python text normalization", "reason": "No existing tool"})
        self.stage(ident, research=True)
        self.stage(ident, DESIGN)
        self.stage(ident, IMPLEMENTATION)
        self.stage(ident)
        self.stage(ident, {"approved": True, "reason": "Tests cover cases"})
        return ident

    def test_complete_workshop_stages_and_reuse(self):
        ident = self.build_to_canary(expected=["a", "b"])
        self.stage(ident)
        self.stage(ident)
        self.stage(ident, {"verified": True, "summary": "Tags normalized"})
        parent = self.store.get(ident)
        self.assertEqual(parent["state"], "completed", parent)
        self.assertEqual(parent["result"]["output"], ["a", "b"])
        self.assertEqual(len(parent["result"]["history"]), 9)
        self.assertEqual(self.registry.list()[0]["status"], "experimental")
        reuse = self.start()
        self.stage(reuse, {"route": "tool", "name": parent["result"]["tool"]})
        self.assertEqual(self.store.get(reuse)["result"]["stage"], "canary")
        self.stage(reuse)
        self.stage(reuse)
        self.stage(reuse, {"verified": True})
        self.assertEqual(self.store.get(reuse)["state"], "completed")

    def test_canary_mismatch_disables_and_stops(self):
        ident = self.build_to_canary(expected=["wrong"])
        self.stage(ident)
        self.assertEqual(self.store.get(ident)["state"], "needs_review")
        self.assertEqual(self.registry.list()[0]["status"], "disabled")

    def test_cancel_propagates_to_child(self):
        ident = self.start()
        child = self.store.get(ident)["result"]["child_task_id"]
        self.store.cancel(ident)
        reconcile_workshops(self.store)
        self.assertEqual(self.store.get(child)["state"], "cancelled")
        self.assertEqual(self.store.get(ident)["state"], "needs_review")

    def test_waiting_workshop_survives_restart_without_duplicate_stage(self):
        ident = self.start()
        child = self.store.get(ident)["result"]["child_task_id"]
        self.store.recover()
        reconcile_workshops(self.store)
        self.assertEqual(self.store.get(ident)["state"], "waiting")
        self.assertEqual(self.store.get(ident)["result"]["child_task_id"], child)
        self.assertEqual(len(self.store.rows("SELECT id FROM tasks WHERE parent_task_id=?", (ident,))), 1)

    def test_interrupted_stage_requires_review_after_restart(self):
        ident = self.start()
        self.store.claim("stage", ["workshop_stage"])
        self.store.recover()
        reconcile_workshops(self.store)
        self.assertEqual(self.store.get(ident)["state"], "needs_review")

    def test_gap_detection_cannot_authorize_host_mutation(self):
        ident = self.start()
        self.stage(ident, {"route": "capability", "name": "terminal.run", "arguments": {}})
        self.assertEqual(self.store.get(ident)["state"], "needs_review")

    def test_existing_capability_skips_tool_generation(self):
        ident = self.start()
        self.stage(ident, {"route": "capability", "name": "filesystem.list", "arguments": {"path": "."}})
        self.assertEqual(self.store.get(ident)["result"]["stage"], "execute")
        self.stage(ident)
        self.stage(ident, {"verified": True})
        self.assertEqual(self.store.get(ident)["state"], "completed")
        self.assertFalse(self.registry.list())

    def test_review_rejection_disables(self):
        ident = self.start()
        self.stage(ident, {"route": "build", "research_query": "text normalization"})
        self.stage(ident, research=True)
        self.stage(ident, DESIGN)
        self.stage(ident, IMPLEMENTATION)
        self.stage(ident)
        self.stage(ident, {"approved": False, "reason": "Insufficient coverage"})
        self.assertEqual(self.store.get(ident)["state"], "needs_review")
        self.assertEqual(self.registry.list()[0]["status"], "disabled")

    def test_model_design_correction_is_bounded_and_audited(self):
        ident = self.start()
        self.stage(ident, {"route": "build", "research_query": "text normalization"})
        self.stage(ident, research=True)
        task = self.store.claim("design", ["workshop_stage"])
        bad = {**DESIGN, "inputs": {"tags": "list"}}
        gateway = FakeGateway(bad, DESIGN)
        result = Workshop(self.db, self.store, gateway, self.root).run_stage(task)
        self.assertEqual(result, DESIGN)
        self.assertEqual(len(self.store.rows("SELECT id FROM audit_events WHERE event_type='workshop_model_correction'")), 1)
        self.assertEqual(self.store.get(task["id"])["result"]["attempt"], 2)

    def test_repeated_invalid_design_stops_after_one_correction(self):
        ident = self.start()
        self.stage(ident, {"route": "build", "research_query": "text normalization"})
        self.stage(ident, research=True)
        task = self.store.claim("design", ["workshop_stage"])
        bad = {**DESIGN, "inputs": {"tags": "list"}}
        gateway = FakeGateway(bad, bad, DESIGN)
        with self.assertRaises(ValueError):
            Workshop(self.db, self.store, gateway, self.root).run_stage(task)
        self.assertEqual(next(gateway.replies), DESIGN)

    def test_broker_can_invoke_retained_tool(self):
        from lilith.capabilities import CapabilityBroker, READ_ONLY
        name = self.make_tool()
        self.approve(name)
        broker = CapabilityBroker(self.db, self.root, allowed=READ_ONLY, task_id=3)
        result = broker.invoke("tool.invoke", {"name": name, "data": {"tags": ["UPPER"]}})
        self.assertEqual(result["output"], ["upper"])

    def test_disabled_tool_cannot_reenter_executive_catalog(self):
        name = self.make_tool()
        self.approve(name)
        self.registry.status(name, "disabled")
        self.assertFalse(self.registry.list(usable=True))


if __name__ == "__main__":
    unittest.main()
