"""Opt-in live acceptance against Ollama/public docs, always using disposable state.

Run explicitly: python tests/live_acceptance.py
Never discovered by the offline unittest suite.
"""
import json
from pathlib import Path
import tempfile
import time

from lilith.config import get_model_name
from lilith.database import Database
from lilith.tasks import TaskStore
from lilith.research import Research
from lilith.tool_registry import ToolRegistry
from lilith.workers import WorkerManager


def main():
    with tempfile.TemporaryDirectory(prefix="lilith-acceptance-") as directory:
        root = Path(directory)
        db = Database(root / "state.db")
        store = TaskStore(db)
        manager = WorkerManager(db, root, get_model_name())
        try:
            search = Research(db, store).search(0, "python arithmetic operators")
            print(json.dumps({"live_search_results": search["results"][:3]}), flush=True)
            ident = store.enqueue("workshop", {
                "request": "Create a pure JSON tool converting Celsius to Fahrenheit using celsius * 9 / 5 + 32. "
                           "Input is an object with required numeric celsius; return an object with numeric fahrenheit. Reject invalid inputs.",
                "data": {"celsius": 0}, "expected": {"fahrenheit": 32},
                "urls": ["https://docs.python.org/3/tutorial/introduction.html"],
                "research_query": "Python numeric arithmetic and JSON data transformations",
            })
            manager.start()
            deadline = time.monotonic() + 900
            previous = None
            while time.monotonic() < deadline:
                task = store.get(ident)
                checkpoint = task["result"] or {}
                stage = (task["state"], checkpoint.get("stage"))
                if stage != previous:
                    print(json.dumps({"state": stage[0], "stage": stage[1]}), flush=True)
                    previous = stage
                if task["state"] in {"completed", "failed", "needs_review", "cancelled"}:
                    print(json.dumps({"final": task}, indent=2), flush=True)
                    if task["state"] != "completed":
                        print(json.dumps(store.rows("SELECT id,type,state,error FROM tasks"), indent=2), flush=True)
                        for row in store.rows("SELECT result FROM tasks WHERE type='workshop_stage' AND state='needs_review'"):
                            print((row['result'] or '')[:5000], flush=True)
                        raise RuntimeError("Live workshop did not complete")
                    name = task["result"]["tool"]
                    result = ToolRegistry(db, store).invoke(name, {"celsius": 100}, task_id=ident)
                    assert result == {"fahrenheit": 212}, result
                    print(json.dumps({"retained_tool_reuse": result, "passed": True}), flush=True)
                    return
                time.sleep(0.5)
            raise TimeoutError("Live acceptance exceeded 15 minutes")
        finally:
            manager.close()
            db.close()


if __name__ == "__main__":
    main()
