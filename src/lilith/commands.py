"""Owner console for persistent background work."""
import json

from lilith.capabilities import READ_ONLY, SPECS

HELP = """/tasks                         List recent tasks
/task <id>                     Show inputs, results, errors, and state
/cancel <id>                   Cancel queued or active work
/goals                         List goals
/goal <owner|shared|self_directed|maintenance> <title>
/goal-state <id> <active|paused|cancelled>  Resume or pause a goal
/priority <task-id> <0-100>      Reprioritize a queued task
/think <request>                Queue bounded planner/reviewer work
/research <question or JSON>    Research public sources with citations
/sources <task-id>              Show saved research provenance
/build-tool <JSON>              Research, build, test, and use a JSON tool
/tools                         List retained tools
/tool <name>                   Inspect manifest and validation report
/tool-run <name> <JSON>         Invoke an experimental/approved tool
/tool-state <name> <status>     Approve, disable, deprecate, or re-enable a tested tool
/capabilities                  List capability argument schemas
/run <capability> <JSON object> Queue an explicitly authorized local action
/journal                       Show recent journal entries
/health                        Show worker heartbeats
/help                          Show this help
/quit                          Exit (dashboard detaches; console stops workers)
/shutdown                      Stop the runtime
Existing /remember, /memories, /self, /affect, /interest commands remain available.
"""


def handle_command(text, store, manager):
    command, _, rest = text.partition(" ")
    if command not in {"/help", "/tasks", "/task", "/cancel", "/goals", "/goal", "/goal-state",
                       "/priority", "/think", "/capabilities", "/run", "/journal", "/health",
                       "/research", "/sources", "/build-tool", "/tools", "/tool", "/tool-run", "/tool-state",
                       "/remember", "/memories", "/self", "/affect", "/interest"}:
        return False
    try:
        if command == "/help":
            print(HELP)
        elif command == "/remember":
            if not rest.strip():
                raise ValueError("Usage: /remember <something>")
            ident = store.db.save_memory(memory_type="episodic", content=rest.strip(), source="owner", confidence=1.0, importance=0.8)
            store.db.audit("memory_created", f"Created memory #{ident}.", "owner")
            print(f"Lilith stored memory #{ident}.")
        elif command == "/memories":
            print(json.dumps(store.db.recent_memories(limit=20), indent=2, ensure_ascii=False))
        elif command in {"/self", "/affect", "/interest"}:
            from lilith.self_state import SelfState
            state = SelfState(store.db)
            if command == "/self":
                print(state.render())
            elif command == "/interest":
                if not rest.strip():
                    raise ValueError("Usage: /interest <topic>")
                state.add_interest(rest.strip())
                store.db.audit("interest_updated", rest.strip(), "owner")
                print(f"Interest strengthened: {rest.strip()}")
            elif not rest.strip():
                print(json.dumps(state.affect(), indent=2))
            else:
                name, raw = rest.split()
                import math
                value = float(raw)
                if not math.isfinite(value):
                    raise ValueError("Affect must be a finite number")
                state.set_affect(name.lower(), value)
                store.db.audit("affect_owner_override", f"{name}: {value}", "owner")
                print(json.dumps(state.affect(), indent=2))
        elif command == "/research":
            payload = json.loads(rest) if rest.lstrip().startswith("{") else {"query": rest}
            if not isinstance(payload, dict) or payload.keys() - {"query", "urls"} or not isinstance(payload.get("query"), str) or not payload["query"].strip():
                raise ValueError('Usage: /research question OR {"query":"question","urls":["https://..."]}')
            print(f"Queued research task #{store.enqueue('research', payload, timeout=240)}.")
        elif command == "/sources":
            from lilith.research import Research
            Research(store.db, store)
            print(json.dumps(store.rows("SELECT id,url,title,retrieved_at,sha256,byte_count,quarantine_path,kind FROM research_sources WHERE task_id=?", (int(rest),)), indent=2))
        elif command == "/build-tool":
            from lilith.workshop import validate_request
            payload = json.loads(rest)
            validate_request(payload)
            print(f"Queued workshop #{store.enqueue('workshop', payload)}.")
        elif command in {"/tools", "/tool", "/tool-run", "/tool-state"}:
            from lilith.tool_registry import ToolRegistry
            registry = ToolRegistry(store.db, store)
            if command == "/tools":
                print(json.dumps(registry.list(), indent=2))
            elif command == "/tool":
                row, manifest, _, _ = registry.load(rest.strip())
                print(json.dumps({"manifest": manifest, "directory": row["directory"],
                                  "report": json.loads(row["report"] or "null")}, indent=2))
            elif command == "/tool-run":
                name, _, raw = rest.partition(" ")
                registry.load(name)
                data = json.loads(raw)
                from lilith.tool_sandbox import bounded
                bounded(data)
                print(f"Queued tool invocation #{store.enqueue('tool_invocation', {'name': name, 'data': data}, timeout=15)}.")
            else:
                name, status = rest.split()
                if status not in {"approved", "experimental", "deprecated", "disabled"}:
                    raise ValueError("Owner statuses: approved, experimental, deprecated, disabled")
                registry.status(name, status)
                print(f"{name}: {status}")
        elif command == "/tasks":
            print(json.dumps(store.rows("SELECT id,type,state,priority,goal_id,error FROM tasks ORDER BY id DESC LIMIT 30"), indent=2))
        elif command == "/task":
            print(json.dumps(store.get(int(rest)), indent=2))
        elif command == "/cancel":
            store.cancel(int(rest))
            print("Cancellation requested.")
        elif command == "/goals":
            print(json.dumps(store.rows("SELECT * FROM goals ORDER BY priority DESC,id LIMIT 100"), indent=2))
        elif command == "/goal":
            kind, _, title = rest.partition(" ")
            print(f"Created goal #{store.goal(title, kind=kind)}.")
        elif command == "/goal-state":
            ident, state = rest.split()
            if state not in {"active", "paused", "cancelled"}:
                raise ValueError("Use active, paused, or cancelled")
            from lilith.database import utc_now
            with store.transaction() as c:
                cur = c.execute("UPDATE goals SET status=?,updated_at=? WHERE id=?", (state, utc_now(), int(ident)))
                if not cur.rowcount:
                    raise ValueError("Unknown goal")
                store._event(c, "goal_status", {"goal_id": int(ident), "state": state})
            if state != "active":
                for task in store.rows("SELECT id FROM tasks WHERE goal_id=? AND state NOT IN ('completed','failed','cancelled','needs_review')", (int(ident),)):
                    store.cancel(task["id"])
            print(f"Goal #{ident}: {state}")
        elif command == "/priority":
            ident, value = map(int, rest.split())
            if not 0 <= value <= 100:
                raise ValueError("Priority must be 0–100")
            with store.transaction() as c:
                cur = c.execute("UPDATE tasks SET priority=? WHERE id=? AND state='queued'", (value, ident))
                if not cur.rowcount:
                    raise ValueError("Task must exist and be queued")
                store._event(c, "task_priority", {"task_id": ident, "priority": value})
        elif command == "/think":
            if not rest.strip():
                raise ValueError("Usage: /think <request>")
            print(f"Queued task #{store.enqueue('executive', {'request': rest})}.")
        elif command == "/capabilities":
            for name, (required, optional) in sorted(SPECS.items()):
                fields = [f"{k}:{v.__name__}" for k, v in required.items()]
                fields += [f"{k}?:{v.__name__}" for k, v in optional.items()]
                print(f"{name}: {', '.join(fields)}" + (" [read-only]" if name in READ_ONLY else ""))
        elif command == "/run":
            name, _, raw = rest.partition(" ")
            args = json.loads(raw)
            if name not in SPECS or not isinstance(args, dict):
                raise ValueError("Unknown capability or invalid arguments")
            # Authorization comes from this explicit owner command, never a model-generated field.
            ident = store.enqueue("capability", {"capability": name, "arguments": args},
                                  priority=80, resumable=name in READ_ONLY, max_attempts=2 if name in READ_ONLY else 1)
            print(f"Queued task #{ident}.")
        elif command == "/journal":
            print(json.dumps(store.rows("SELECT * FROM journal_entries ORDER BY id DESC LIMIT 10"), indent=2))
        elif command == "/health":
            print(json.dumps({"supervisor_error": manager.error,
                              "workers": store.rows("SELECT * FROM workers ORDER BY heartbeat DESC LIMIT 20")}, indent=2))
    except (ValueError, TypeError, KeyError, OSError) as error:
        print(f"Command error: {error}")
    return True
