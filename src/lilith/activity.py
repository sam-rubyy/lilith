"""Observable work summaries, with progress based on completed stages."""
import json

from lilith.tasks import TERMINAL
from lilith.workshop import STAGES

LABELS = {"gap": "Checking existing tools", "research": "Researching sources",
          "design": "Designing the tool", "implementation": "Writing and saving code",
          "test": "Running tests", "review": "Reviewing implementation",
          "canary": "Checking sample input", "execute": "Running the tool", "verify": "Verifying output"}


def work_progress(store, task):
    result = task["result"] or {}
    if task["type"] != "workshop":
        return (100 if task["state"] == "completed" else None), task["state"]
    history = result.get("history", {})
    route = None
    if "gap" in history:
        route = (store.get(history["gap"])["result"] or {}).get("route")
    stages = {"tool": ["gap", "canary", "execute", "verify"],
              "capability": ["gap", "execute", "verify"]}.get(route, STAGES)
    completed = len(set(history) & set(stages))
    child_id = result.get("child_task_id")
    if child_id and result.get("stage") not in history and store.get(child_id)["state"] == "completed":
        completed += 1
    percent = 100 if task["state"] == "completed" else min(99, int(100 * completed / len(stages)))
    label = LABELS.get(result.get("stage"), "Queued")
    if task["state"] in TERMINAL:
        label = task["state"].replace("_", " ")
    return percent, label


def activity_text(store, task):
    percent, label = work_progress(store, task)
    lines = [f"#{task['id']} · {task['type']} · {label}",
             task["input"].get("request", task["input"].get("query", task["input"].get("name", task["input"].get("capability", ""))))]
    if percent is not None:
        lines.append(f"{percent}% of stages complete")
    children = store.rows("SELECT id FROM tasks WHERE parent_task_id=? AND type != 'journal' ORDER BY id", (task["id"],))
    for row in children:
        child = store.get(row["id"])
        stage = child["input"].get("stage", child["type"])
        lines.append(f"\n{LABELS.get(stage, stage)} · {child['state']}")
        if child["result"]:
            # Recorded outputs and short explanations; never private model reasoning.
            lines.append(json.dumps(child["result"], ensure_ascii=False, indent=2)[:5000])
            if stage == "implementation" and child["result"].get("name"):
                from lilith.tool_registry import ToolRegistry
                try:
                    _, _, source, _ = ToolRegistry(store.db, store).load(child["result"]["name"])
                    lines.append("Saved implementation\n" + source[:6000])
                except (ValueError, OSError) as error:
                    lines.append(f"Implementation unavailable: {error}")
        if child["error"]:
            lines.append(child["error"])
    if task["result"] and (not children or task["state"] in TERMINAL):
        lines.append("\nResult\n" + json.dumps(task["result"], ensure_ascii=False, indent=2)[:6000])
    if task["error"]:
        lines.append("\n" + task["error"])
    if task.get("worker_log"):
        lines.append("\nWorker log: " + task["worker_log"])
    return "\n".join(lines)
