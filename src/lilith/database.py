from __future__ import annotations

import sqlite3
import time

from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from lilith.migrations import migrate
from lilith.redaction import audit_message


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()

        self.connection = sqlite3.connect(
            path,
            check_same_thread=False,
            timeout=30,
        )

        try:
            with self.lock:
                self.connection.execute("PRAGMA busy_timeout = 30000")
                deadline = time.monotonic() + 30
                while True:
                    try:
                        self.connection.execute("PRAGMA journal_mode = WAL")
                        break
                    except sqlite3.OperationalError as error:
                        # Concurrent first-open journal changes can return BUSY
                        # immediately despite the connection's busy timeout.
                        if (getattr(error, "sqlite_errorcode", 0) & 255 not in
                                {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or time.monotonic() >= deadline):
                            raise
                        time.sleep(0.05)
                self.connection.execute("PRAGMA foreign_keys = ON")
                migrate(self.connection, utc_now())
        except BaseException:
            self.connection.close()
            raise

    # ---------------------------------------------------------
    # Audit
    # ---------------------------------------------------------

    def audit(
        self,
        event_type: str,
        message: str,
        actor: str = "system",
    ) -> None:
        with self.lock:
            self.connection.execute(
                """
                INSERT INTO audit_events (
                    timestamp,
                    event_type,
                    actor,
                    message
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    event_type,
                    actor,
                    audit_message(message),
                ),
            )

            self.connection.commit()

    # ---------------------------------------------------------
    # Journal
    # ---------------------------------------------------------

    def journal(
        self,
        body: str,
        title: str | None = None,
        author: str = "lilith",
    ) -> None:
        with self.lock:
            self.connection.execute(
                """
                INSERT INTO journal_entries (
                    timestamp,
                    author,
                    title,
                    body
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    author,
                    title,
                    body,
                ),
            )

            self.connection.commit()

    # ---------------------------------------------------------
    # Chat
    # ---------------------------------------------------------

    def save_message(
        self,
        role: str,
        content: str,
        model: str | None = None,
    ) -> int:
        with self.lock:
            cursor = self.connection.execute(
                """
                INSERT INTO chat_messages (
                    timestamp,
                    role,
                    content,
                    model
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    role,
                    content,
                    model,
                ),
            )

            self.connection.commit()
            return int(cursor.lastrowid)

    def recent_messages(
        self,
        limit: int = 20,
    ) -> list[dict[str, str]]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT role, content
                FROM chat_messages
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        rows.reverse()

        return [
            {
                "role": role,
                "content": content,
            }
            for role, content in rows
        ]

    # ---------------------------------------------------------
    # Memories
    # ---------------------------------------------------------

    def save_memory(
        self,
        memory_type: str,
        content: str,
        source: str,
        confidence: float = 1.0,
        importance: float = 0.5,
        owner_message_id: int | None = None,
        lilith_message_id: int | None = None,
    ) -> int:
        with self.lock:
            cursor = self.connection.execute(
                """
                INSERT INTO memories (
                    timestamp,
                    memory_type,
                    content,
                    source,
                    confidence,
                    importance,
                    owner_message_id,
                    lilith_message_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    memory_type,
                    content,
                    source,
                    confidence,
                    importance,
                    owner_message_id,
                    lilith_message_id,
                ),
            )

            self.connection.commit()

            return int(cursor.lastrowid)

    def recent_memories(
        self,
        limit: int = 10,
    ) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT
                    id,
                    timestamp,
                    memory_type,
                    content,
                    source,
                    confidence,
                    importance,
                    owner_message_id,
                    lilith_message_id
                FROM memories
                WHERE active = 1
                ORDER BY importance DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "id": row[0],
                "timestamp": row[1],
                "type": row[2],
                "content": row[3],
                "source": row[4],
                "confidence": row[5],
                "importance": row[6],
                "owner_message_id": row[7],
                "lilith_message_id": row[8],
            }
            for row in rows
        ]

    # ---------------------------------------------------------
    # Affect
    # ---------------------------------------------------------

    def set_affect(
        self,
        name: str,
        value: float,
    ) -> None:
        with self.lock:
            self.connection.execute(
                """
                INSERT INTO affect_state (
                    name,
                    value,
                    updated_at
                )
                VALUES (?, ?, ?)

                ON CONFLICT(name)
                DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (
                    name,
                    value,
                    utc_now(),
                ),
            )

            self.connection.commit()

    def get_affect(
        self,
        name: str,
    ) -> float | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT value
                FROM affect_state
                WHERE name = ?
                """,
                (name,),
            ).fetchone()

        if row is None:
            return None

        return float(row[0])

    def adjust_affect(self, name: str, amount: float, default: float) -> None:
        with self.lock:
            self.connection.execute(
                """INSERT INTO affect_state(name,value,updated_at) VALUES (?,MAX(0,MIN(1,?+?)),?)
                ON CONFLICT(name) DO UPDATE SET value=MAX(0,MIN(1,affect_state.value+?)),updated_at=excluded.updated_at""",
                (name, default, amount, utc_now(), amount),
            )
            self.connection.commit()

    def get_all_affect(self) -> dict[str, float]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT name, value
                FROM affect_state
                ORDER BY name
                """
            ).fetchall()

        return {
            name: float(value)
            for name, value in rows
        }

    # ---------------------------------------------------------
    # Interests
    # ---------------------------------------------------------

    def adjust_interest(
        self,
        topic: str,
        amount: float,
    ) -> None:
        topic = topic.strip()

        if not topic:
            return

        with self.lock:
            row = self.connection.execute(
                """
                SELECT fascination
                FROM interests
                WHERE topic = ?
                """,
                (topic,),
            ).fetchone()

            now = utc_now()

            if row is None:
                fascination = max(
                    0.0,
                    min(1.0, 0.5 + amount),
                )

                self.connection.execute(
                    """
                    INSERT INTO interests (
                        topic,
                        fascination,
                        first_seen,
                        last_seen
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        topic,
                        fascination,
                        now,
                        now,
                    ),
                )

            else:
                fascination = max(
                    0.0,
                    min(
                        1.0,
                        float(row[0]) + amount,
                    ),
                )

                self.connection.execute(
                    """
                    UPDATE interests
                    SET
                        fascination = ?,
                        last_seen = ?
                    WHERE topic = ?
                    """,
                    (
                        fascination,
                        now,
                        topic,
                    ),
                )

            self.connection.commit()

    def get_interests(
        self,
        limit: int = 10,
    ) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT
                    topic,
                    fascination,
                    first_seen,
                    last_seen
                FROM interests
                ORDER BY fascination DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "topic": row[0],
                "fascination": float(row[1]),
                "first_seen": row[2],
                "last_seen": row[3],
            }
            for row in rows
        ]

    # ---------------------------------------------------------
    # Identity
    # ---------------------------------------------------------

    def ensure_identity(
        self,
        key: str,
        value: str,
        source: str,
    ) -> None:
        with self.lock:
            existing = self.connection.execute(
                """
                SELECT value
                FROM identity_state
                WHERE key = ?
                """,
                (key,),
            ).fetchone()

            if existing is not None:
                return

            now = utc_now()

            self.connection.execute(
                """
                INSERT INTO identity_state (
                    key,
                    value,
                    updated_at,
                    source
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    key,
                    value,
                    now,
                    source,
                ),
            )

            self.connection.execute(
                """
                INSERT INTO identity_history (
                    timestamp,
                    key,
                    old_value,
                    new_value,
                    source
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    now,
                    key,
                    None,
                    value,
                    source,
                ),
            )

            self.connection.commit()

    def set_identity(
        self,
        key: str,
        value: str,
        source: str,
    ) -> None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT value
                FROM identity_state
                WHERE key = ?
                """,
                (key,),
            ).fetchone()

            old_value = (
                row[0]
                if row is not None
                else None
            )

            now = utc_now()

            self.connection.execute(
                """
                INSERT INTO identity_state (
                    key,
                    value,
                    updated_at,
                    source
                )
                VALUES (?, ?, ?, ?)

                ON CONFLICT(key)
                DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    source = excluded.source
                """,
                (
                    key,
                    value,
                    now,
                    source,
                ),
            )

            self.connection.execute(
                """
                INSERT INTO identity_history (
                    timestamp,
                    key,
                    old_value,
                    new_value,
                    source
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    now,
                    key,
                    old_value,
                    value,
                    source,
                ),
            )

            self.connection.commit()

    def get_identity(self) -> dict[str, str]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT key, value
                FROM identity_state
                ORDER BY key
                """
            ).fetchall()

        return {
            key: value
            for key, value in rows
        }

    # ---------------------------------------------------------
    # Self-beliefs
    # ---------------------------------------------------------
    def memory_exists(
        self,
        content: str,
    ) -> bool:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT 1
                FROM memories
                WHERE active = 1
                AND LOWER(content) = LOWER(?)
                LIMIT 1
                """,
                (content.strip(),),
            ).fetchone()

        return row is not None


    def self_belief_exists(
        self,
        belief: str,
    ) -> bool:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT 1
                FROM self_beliefs
                WHERE active = 1
                AND LOWER(belief) = LOWER(?)
                LIMIT 1
                """,
                (belief.strip(),),
            ).fetchone()

        return row is not None

    def add_self_belief(
        self,
        belief: str,
        confidence: float,
        source: str,
    ) -> int:
        with self.lock:
            cursor = self.connection.execute(
                """
                INSERT INTO self_beliefs (
                    timestamp,
                    belief,
                    confidence,
                    source
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    belief,
                    confidence,
                    source,
                ),
            )

            self.connection.commit()

            return int(cursor.lastrowid)

    def get_self_beliefs(
        self,
        limit: int = 10,
    ) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT
                    id,
                    belief,
                    confidence,
                    source,
                    timestamp
                FROM self_beliefs
                WHERE active = 1
                ORDER BY confidence DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "id": row[0],
                "belief": row[1],
                "confidence": float(row[2]),
                "source": row[3],
                "timestamp": row[4],
            }
            for row in rows
        ]

    # ---------------------------------------------------------

    def close(self) -> None:
        with self.lock:
            self.connection.close()
