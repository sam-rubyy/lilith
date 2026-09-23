from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from lilith.config import get_database_path


TABLES_TO_CLEAR = [
    "model_lease",
    "model_requests",
    "conversation_metrics",
    "curiosity_sessions",
    "runtime_settings",
    "runtime_status",
    "inbox",
    "tools",
    "research_sources",
    "workers",
    "tasks",
    "goals",
    "chat_messages",
    "memories",
    "journal_entries",
    "affect_state",
    "interests",
    "identity_state",
    "identity_history",
    "self_beliefs",
    "audit_events",
]


def _connect(path):
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def create_backup(database_path: Path) -> Path:
    backup_dir = database_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")

    backup_path = backup_dir / f"pre_lobotomy_{timestamp}.db"

    source = _connect(database_path)
    target = _connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    return backup_path


def clear_database(database_path: Path) -> None:
    connection = _connect(database_path)

    try:
        connection.execute("PRAGMA foreign_keys = OFF")

        for table in TABLES_TO_CLEAR:
            exists = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                AND name = ?
                """,
                (table,),
            ).fetchone()

            if exists:
                connection.execute(f'DELETE FROM "{table}"')

        # Reset AUTOINCREMENT counters.
        sequence_exists = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            AND name = 'sqlite_sequence'
            """
        ).fetchone()

        if sequence_exists:
            for table in TABLES_TO_CLEAR:
                connection.execute(
                    """
                    DELETE FROM sqlite_sequence
                    WHERE name = ?
                    """,
                    (table,),
                )

        connection.commit()

        connection.execute("VACUUM")

    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Owner-only Lilith state reset tool. "
            "Clears conversations, memories, journal entries, "
            "and audit history."
        )
    )

    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation.",
    )

    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create a recovery copy before wiping.",
    )

    args = parser.parse_args()

    database_path = get_database_path()

    if not database_path.exists():
        print(f"No Lilith database found at:")
        print(database_path)
        return

    print()
    print("======================================")
    print("       LILITH LOBOTOMIZER")
    print("======================================")
    print()
    print(f"Database: {database_path}")
    print()
    print("This will erase:")
    print("  - conversation history")
    print("  - persistent memories")
    print("  - journal entries")
    print("  - audit history")
    print()

    if not args.yes:
        confirmation = input(
            'Type "LOBOTOMIZE" to continue: '
        ).strip()

        if confirmation != "LOBOTOMIZE":
            print()
            print("Lobotomy cancelled.")
            return

    from lilith.workers import RuntimeLock
    lock = RuntimeLock(database_path)
    try:
        if not args.no_backup:
            backup_path = create_backup(database_path)
            print(f"Recovery snapshot created: {backup_path}")
        clear_database(database_path)
    finally:
        lock.close()

    print()
    print("======================================")
    print("     LOBOTOMY COMPLETE")
    print("======================================")
    print()
    print("Lilith's persistent state has been cleared.")
    print("Database schema remains intact.")
    print()


if __name__ == "__main__":
    main()
