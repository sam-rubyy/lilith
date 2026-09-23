"""Persistent, hash-verified registry for capability-free experimental JSON tools."""
import hashlib
import json
from pathlib import Path
import re
import uuid

from lilith.database import utc_now
from lilith.tool_sandbox import Sandbox, SandboxError, bounded, formatted_source

SCHEMA = """
CREATE TABLE IF NOT EXISTS tools (
 name TEXT PRIMARY KEY, version TEXT NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, task_id INTEGER NOT NULL,
 manifest TEXT NOT NULL, directory TEXT NOT NULL, hash TEXT NOT NULL, report TEXT
);
"""
STATUSES = {"draft", "testing", "experimental", "approved", "deprecated", "disabled"}
SCHEMA_KEYS = {"type", "description", "properties", "required", "additionalProperties", "items", "enum", "const",
               "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum"}
TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}


def check_schema(schema, depth=0):
    if depth > 10 or not isinstance(schema, dict) or schema.keys() - SCHEMA_KEYS or not isinstance(schema.get("type"), str) or schema.get("type") not in TYPES:
        raise ValueError("Tool schemas require a supported type and only the documented JSON Schema subset: " + str(schema)[:500])
    if "description" in schema and not isinstance(schema["description"], str):
        raise ValueError("Schema description must be a string")
    for key in {"minItems", "maxItems", "minLength", "maxLength"} & schema.keys():
        if type(schema[key]) is not int or not 0 <= schema[key] <= 65536:
            raise ValueError("Invalid schema length limit")
    for key in {"minimum", "maximum"} & schema.keys():
        if type(schema[key]) not in {int, float}:
            raise ValueError("Invalid numeric schema limit")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("enum must be a nonempty list")
    if schema["type"] == "object":
        props = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(props, dict) or not isinstance(required, list) or any(type(k) is not str or k not in props for k in required):
            raise ValueError("Invalid object schema")
        if type(schema.get("additionalProperties", False)) is not bool:
            raise ValueError("additionalProperties must be boolean")
        for sub in props.values():
            check_schema(sub, depth + 1)
    if schema["type"] == "array":
        check_schema(schema.get("items"), depth + 1)
    bounded(schema)


def validate_value(value, schema, path="$"):
    kind = schema["type"]
    types = TYPES[kind]
    types = types if isinstance(types, tuple) else (types,)
    if type(value) not in types:
        raise SandboxError(f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"] or "const" in schema and value != schema["const"]:
        raise SandboxError(f"{path}: value is not allowed")
    if kind in {"string", "array"}:
        suffix = "Length" if kind == "string" else "Items"
        if len(value) < schema.get("min" + suffix, 0) or len(value) > schema.get("max" + suffix, 65536):
            raise SandboxError(f"{path}: length outside schema limits")
    if kind in {"number", "integer"} and (value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf"))):
        raise SandboxError(f"{path}: numeric value outside schema limits")
    if kind == "object":
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys():
            raise SandboxError(f"{path}: missing required property")
        if not schema.get("additionalProperties", False) and value.keys() - properties.keys():
            raise SandboxError(f"{path}: unknown property")
        for key in value.keys() & properties.keys():
            validate_value(value[key], properties[key], path + "." + key)
    if kind == "array":
        for i, item in enumerate(value):
            validate_value(item, schema["items"], f"{path}[{i}]")


def artifact_hash(manifest, source, tests, readme):
    stable = {k: v for k, v in manifest.items() if k not in {"hash", "status"}}
    blob = json.dumps({"manifest": stable, "source": source, "tests": tests, "readme": readme}, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


class ToolRegistry:
    def __init__(self, database, store):
        self.db, self.store = database, store
        self.root = database.path.parent / "tools" / "experimental"
        with database.lock:
            database.connection.executescript(SCHEMA)
            database.connection.commit()

    def list(self, usable=False):
        return self.store.rows("SELECT name,version,purpose,status,hash FROM tools" +
                               (" WHERE status IN ('experimental','approved')" if usable else "") + " ORDER BY name")

    def create(self, task_id, design, implementation):
        slug = design.get("name", "")
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,39}", slug):
            raise ValueError("Tool name must be a short lowercase slug")
        name = f"{slug}-{task_id}-{uuid.uuid4().hex[:6]}"
        if design.get("permissions") != []:
            raise ValueError("Generated tools cannot request host capabilities in v0.1.0")
        for key in ("inputs", "outputs"):
            check_schema(design[key])
        purpose = design.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 2000:
            raise ValueError("Tool purpose is required")
        source = formatted_source(implementation["source"])
        tests = implementation.get("tests")
        if not isinstance(tests, list) or not 3 <= len(tests) <= 20:
            raise ValueError("Generate 3–20 tests, including successful and failure cases")
        for case in tests:
            if not isinstance(case, dict) or "input" not in case or ("expected" in case) == (case.get("expect_error") is True):
                raise ValueError("Each test requires input and either expected output or expect_error=true")
            bounded(case)
            if "expected" in case:
                validate_value(case["input"], design["inputs"])
                validate_value(case["expected"], design["outputs"])
        if not any(t.get("expect_error") is True for t in tests) or not any("expected" in t for t in tests):
            raise ValueError("Tests must include success and failure paths")
        readme = implementation.get("readme", "")
        if not isinstance(readme, str) or not readme.strip() or len(readme) > 10000:
            raise ValueError("A bounded README is required")
        now = utc_now()
        manifest = {"name": name, "version": "0.1.0", "purpose": purpose, "author": "Lilith",
                    "created_at": now, "inputs": design["inputs"], "outputs": design["outputs"],
                    "permissions": [], "resource_limits": {"operations": 20000, "json_bytes": 65536,
                    "network_bytes": 0, "host_writes": 0}, "timeout": 2,
                    "rollback_strategy": "Pure transformation; disable registry entry to withdraw capability",
                    "runtime": "lilith-json-python-v1", "status": "draft"}
        digest = artifact_hash(manifest, source, tests, readme)
        manifest["hash"] = digest
        directory = self.root / name
        self.db.audit("tool_artifact_requested", json.dumps({"task_id": task_id, "name": name, "hash": digest}))
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "tests").mkdir()
        (directory / "tool.py").write_text(source, encoding="utf-8")
        (directory / "tests" / "cases.json").write_text(json.dumps(tests, indent=2), encoding="utf-8")
        (directory / "README.md").write_text(readme, encoding="utf-8")
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        with self.store.transaction() as c:
            c.execute("INSERT INTO tools VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (name, "0.1.0", purpose, "draft", now, now, task_id, json.dumps(manifest), str(directory), digest, None))
            self.store._event(c, "tool_draft_created", {"task_id": task_id, "name": name, "hash": digest})
        return {"name": name, "hash": digest, "directory": str(directory), "manifest": manifest}

    def load(self, name):
        rows = self.store.rows("SELECT * FROM tools WHERE name=?", (name,))
        if not rows:
            raise ValueError("Unknown tool")
        row = rows[0]
        directory = Path(row["directory"]).resolve()
        if not directory.is_relative_to(self.root.resolve()):
            raise ValueError("Tool directory escaped the registry")
        files = [directory / "manifest.json", directory / "tool.py", directory / "tests" / "cases.json", directory / "README.md"]
        if any(not p.resolve().is_relative_to(directory) or p.stat().st_size > 262144 for p in files):
            raise ValueError("Invalid or oversized tool artifact")
        manifest = json.loads(files[0].read_text(encoding="utf-8"))
        source = files[1].read_text(encoding="utf-8")
        tests = json.loads(files[2].read_text(encoding="utf-8"))
        readme = files[3].read_text(encoding="utf-8")
        if artifact_hash(manifest, source, tests, readme) != row["hash"] or manifest.get("hash") != row["hash"]:
            raise ValueError("Tool artifact hash mismatch; rebuild instead of executing modified code")
        if manifest != json.loads(row["manifest"]) or manifest.get("status") != row["status"]:
            raise ValueError("Tool manifest does not match the persistent registry")
        return row, manifest, source, tests

    def status(self, name, status, report=None):
        if status not in STATUSES:
            raise ValueError("Invalid tool status")
        row, manifest, _, _ = self.load(name)
        if status in {"experimental", "approved"}:
            evidence = report if report is not None else json.loads(row["report"] or "{}")
            if evidence.get("passed") is not True or evidence.get("review_approved") is not True:
                raise ValueError("Tool has not passed tests and capability review")
        manifest["status"] = status
        # A crash between file/DB writes fails closed because load requires exact agreement.
        (Path(row["directory"]) / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        with self.store.transaction() as c:
            c.execute("UPDATE tools SET status=?,updated_at=?,manifest=?,report=COALESCE(?,report) WHERE name=?",
                      (status, utc_now(), json.dumps(manifest), json.dumps(report) if report is not None else None, name))
            self.store._event(c, "tool_status", {"name": name, "status": status})

    def invoke(self, name, data, *, task_id, testing=False):
        self.db.audit("tool_invocation_requested", json.dumps({"task_id": task_id, "name": name, "testing": testing}))
        try:
            row, manifest, source, _ = self.load(name)
            allowed = {"testing"} if testing else {"experimental", "approved"}
            if row["status"] not in allowed:
                raise ValueError("Tool is not eligible for invocation")
            bounded(data)
            validate_value(data, manifest["inputs"])
            result = Sandbox().run(source, data)
            validate_value(result, manifest["outputs"])
            self.db.audit("tool_invocation_completed", json.dumps({"task_id": task_id, "name": name,
                          "hash": row["hash"], "result": result}))
            return result
        except Exception as error:
            self.db.audit("tool_invocation_failed", json.dumps({"task_id": task_id, "name": name, "error": str(error)}))
            raise

    def test(self, name, *, task_id):
        self.status(name, "testing")
        _, _, source, tests = self.load(name)
        results = []
        for i, case in enumerate(tests):
            try:
                result = self.invoke(name, case["input"], task_id=task_id, testing=True)
                passed = "expected" in case and result == case["expected"]
                results.append({"case": i, "passed": passed, "result": result})
            except SandboxError as error:
                results.append({"case": i, "passed": case.get("expect_error") is True, "error": str(error)})
        report = {"passed": all(r["passed"] for r in results), "cases": results,
                  "format_check": source == formatted_source(source), "syntax_and_capability_check": True,
                  "input_output_type_checks": True, "sandbox": "lilith-json-python-v1"}
        with self.store.transaction() as c:
            c.execute("UPDATE tools SET report=?,updated_at=? WHERE name=?", (json.dumps(report), utc_now(), name))
        self.db.audit("tool_tests_completed", json.dumps({"task_id": task_id, "name": name, "report": report}))
        if not report["passed"]:
            self.status(name, "disabled", report)
        return report
