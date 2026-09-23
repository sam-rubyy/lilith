"""Ordered, transactional SQLite schema migrations."""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


BASELINE = (
    """CREATE TABLE IF NOT EXISTS audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        event_type TEXT NOT NULL, actor TEXT NOT NULL, message TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS journal_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        author TEXT NOT NULL, title TEXT, body TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        role TEXT NOT NULL, content TEXT NOT NULL, model TEXT)""",
    """CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        memory_type TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 1.0, importance REAL NOT NULL DEFAULT 0.5,
        active INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS affect_state (
        name TEXT PRIMARY KEY, value REAL NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS interests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL UNIQUE,
        fascination REAL NOT NULL DEFAULT 0.5, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS identity_state (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL, source TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS identity_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, key TEXT NOT NULL,
        old_value TEXT, new_value TEXT NOT NULL, source TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS self_beliefs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, belief TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.5, source TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, title TEXT NOT NULL,
        description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', priority INTEGER NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, motivation TEXT NOT NULL,
        parent_goal INTEGER REFERENCES goals(id), progress REAL NOT NULL DEFAULT 0,
        next_action TEXT NOT NULL DEFAULT '', resource_budget TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
        priority INTEGER NOT NULL, origin TEXT NOT NULL, goal_id INTEGER REFERENCES goals(id),
        created_at TEXT NOT NULL, started_at TEXT, updated_at TEXT NOT NULL, finished_at TEXT,
        worker TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
        input TEXT NOT NULL, result TEXT, error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
        parent_task_id INTEGER REFERENCES tasks(id), resumable INTEGER NOT NULL DEFAULT 0,
        timeout REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS task_queue ON tasks(state, priority DESC, id)",
    """CREATE UNIQUE INDEX IF NOT EXISTS goal_active_task ON tasks(goal_id)
        WHERE goal_id IS NOT NULL AND state NOT IN ('completed','failed','cancelled','needs_review')""",
    """CREATE TABLE IF NOT EXISTS workers (
        id TEXT PRIMARY KEY, task_id INTEGER, heartbeat TEXT NOT NULL, state TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS runtime_status (
        id INTEGER PRIMARY KEY CHECK(id=1), pid INTEGER NOT NULL, process_created REAL NOT NULL,
        heartbeat TEXT NOT NULL, state TEXT NOT NULL, stop_requested INTEGER NOT NULL DEFAULT 0,
        model TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS inbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        message TEXT NOT NULL, response TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'queued',
        task_id INTEGER, error TEXT)""",
    "CREATE TABLE IF NOT EXISTS runtime_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS curiosity_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, task_id INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS research_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, requested_url TEXT NOT NULL,
        url TEXT NOT NULL, retrieved_at TEXT NOT NULL, title TEXT NOT NULL, content_type TEXT NOT NULL,
        sha256 TEXT NOT NULL, byte_count INTEGER NOT NULL, text TEXT NOT NULL, links TEXT NOT NULL,
        quarantine_path TEXT, kind TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS research_task ON research_sources(task_id,id)",
    """CREATE TABLE IF NOT EXISTS tools (
        name TEXT PRIMARY KEY, version TEXT NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, task_id INTEGER NOT NULL,
        manifest TEXT NOT NULL, directory TEXT NOT NULL, hash TEXT NOT NULL, report TEXT)""",
)

MODEL_ARBITRATION = (
    """CREATE TABLE model_requests (
        id TEXT PRIMARY KEY, role TEXT NOT NULL, priority INTEGER NOT NULL,
        task_id INTEGER, created_at TEXT NOT NULL, acquired_at TEXT,
        released_at TEXT, state TEXT NOT NULL)""",
    "CREATE INDEX model_request_queue ON model_requests(state,priority DESC,created_at,id)",
    """CREATE TABLE model_lease (
        id INTEGER PRIMARY KEY CHECK(id=1), request_id TEXT NOT NULL,
        role TEXT NOT NULL, acquired_at TEXT NOT NULL, heartbeat TEXT NOT NULL,
        expires_at TEXT NOT NULL)""",
)

CONVERSATION_TELEMETRY = (
    """CREATE TABLE conversation_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER,
        created_at TEXT NOT NULL, router_used INTEGER NOT NULL,
        router_duration REAL NOT NULL, time_to_first_token REAL,
        total_conversation_duration REAL NOT NULL, succeeded INTEGER NOT NULL)""",
    "CREATE INDEX conversation_metrics_recent ON conversation_metrics(id DESC)",
)

WORKER_LOGS = (
    "ALTER TABLE tasks ADD COLUMN worker_log TEXT",
)

MIGRATIONS = (
    Migration(1, "v0_1_0_baseline", BASELINE),
    Migration(2, "model_arbitration", MODEL_ARBITRATION),
    Migration(3, "conversation_telemetry", CONVERSATION_TELEMETRY),
    Migration(4, "worker_logs", WORKER_LOGS),
)


def migrate(connection: sqlite3.Connection, now: str) -> None:
    """Apply every pending migration atomically, one transaction per version."""
    connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)""")
    connection.commit()
    for migration in MIGRATIONS:
        try:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (migration.version,)
            ).fetchone()
            if exists:
                connection.commit()
                continue
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,applied_at) VALUES (?,?,?)",
                (migration.version, migration.name, now),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def current_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0])
