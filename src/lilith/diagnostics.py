"""Secret-free runtime health snapshots shared by console and Textual UI."""
from datetime import datetime, timezone
import os
from pathlib import Path

from lilith.config import get_model_name
from lilith.migrations import current_version


def health_snapshot(store, *, runtime=None, settings=None, supervisor_error=None):
    db = store.db
    workers = store.rows("SELECT * FROM workers ORDER BY heartbeat DESC LIMIT 20")
    lane_names = ("conversation", "reflection", "journal", "work", "research", "workshop")
    lanes = []
    for name in lane_names:
        matches = [row for row in workers if row["id"].startswith(name + "-")]
        active = next((row for row in matches if row["state"] == "running"), None)
        lanes.append({"lane": name, "state": "running" if active else "idle",
                      "task_id": active["task_id"] if active else None,
                      "worker_id": active["id"] if active else None})
    now = datetime.now(timezone.utc)
    stale = [row for row in workers if (now - datetime.fromisoformat(row["heartbeat"])).total_seconds() > 60]
    lease = store.rows("SELECT role,request_id,acquired_at,heartbeat,expires_at FROM model_lease WHERE id=1")
    queue = store.rows("""SELECT role,priority,COUNT(*) AS count FROM model_requests
                           WHERE state='waiting' GROUP BY role,priority ORDER BY priority DESC""")
    recent_failures = store.rows("""SELECT id,type,state,error,worker_log,finished_at FROM tasks
        WHERE state IN ('failed','needs_review') ORDER BY id DESC LIMIT 8""")
    metrics = store.rows("""SELECT router_used,router_duration,time_to_first_token,total_conversation_duration,
                              succeeded,created_at FROM conversation_metrics ORDER BY id DESC LIMIT 10""")
    last_curiosity = store.rows("SELECT created_at,task_id FROM curiosity_sessions ORDER BY id DESC LIMIT 1")
    counts = {
        "tools": store.rows("SELECT COUNT(*) AS count FROM tools")[0]["count"],
        "memories": store.rows("SELECT COUNT(*) AS count FROM memories WHERE active=1")[0]["count"],
        "journals": store.rows("SELECT COUNT(*) AS count FROM journal_entries")[0]["count"],
    }
    return {
        "service": runtime or {"state": "unknown"},
        "supervisor_error": supervisor_error,
        "model": get_model_name(),
        "model_queue": queue,
        "active_model_role": lease[0]["role"] if lease else None,
        "model_lease": lease[0] if lease else None,
        "worker_lanes": lanes,
        "current_tasks": store.rows("""SELECT id,type,state,priority,origin FROM tasks
            WHERE state NOT IN ('completed','failed','cancelled','needs_review') ORDER BY priority DESC,id"""),
        "stale_workers": stale,
        "database_schema_version": current_version(db.connection),
        "wal_status": db.connection.execute("PRAGMA journal_mode").fetchone()[0],
        "workspace": str(Path(os.environ.get("LILITH_WORKSPACE", db.path.parent / "workspace")).expanduser().resolve()),
        "counts": counts,
        "recent_worker_failures": recent_failures,
        "conversation_performance": metrics,
        "curiosity_enabled": settings.get("curiosity_enabled") if settings else None,
        "last_curiosity_session": last_curiosity[0] if last_curiosity else None,
    }
