import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from lilith.database import Database
from lilith.migrations import BASELINE, MIGRATIONS, Migration, current_version, migrate


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_new_database_applies_each_migration_once_and_configures_connection(self):
        db = Database(self.path)
        try:
            rows = db.connection.execute(
                "SELECT version,name FROM schema_migrations ORDER BY version"
            ).fetchall()
            self.assertEqual(rows, [(m.version, m.name) for m in MIGRATIONS])
            self.assertEqual(db.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(db.connection.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
        finally:
            db.close()
        reopened = Database(self.path)
        try:
            self.assertEqual(reopened.connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0], len(MIGRATIONS))
        finally:
            reopened.close()

    def test_foreign_keys_are_enforced(self):
        db = Database(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                db.connection.execute("""INSERT INTO tasks(
                    type,priority,origin,goal_id,created_at,updated_at,input,max_attempts,timeout
                ) VALUES ('executive',50,'owner',999,'now','now','{}',1,10)""")
        finally:
            db.close()

    def test_upgrade_adopts_v0_1_0_tables_without_losing_data(self):
        connection = sqlite3.connect(self.path)
        for statement in BASELINE:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO chat_messages(timestamp,role,content) VALUES ('old','user','keep me')"
        )
        connection.execute("""INSERT INTO memories(
            timestamp,memory_type,content,source,confidence,importance
        ) VALUES ('old','preference','keep this memory','owner',1,0.8)""")
        connection.execute("""INSERT INTO goals(
            id,type,title,description,priority,created_at,updated_at,motivation,resource_budget
        ) VALUES (7,'owner','legacy goal','keep it',80,'old','old','owner','{}')""")
        connection.execute("""INSERT INTO tasks(
            id,type,state,priority,origin,goal_id,created_at,updated_at,input,max_attempts,timeout
        ) VALUES (9,'executive','completed',80,'owner',7,'old','old','{}',1,30)""")
        connection.execute("""INSERT INTO research_sources(
            task_id,requested_url,url,retrieved_at,title,content_type,sha256,byte_count,text,links,kind
        ) VALUES (9,'https://example.com','https://example.com','old','Example','text/plain','abc',4,'text','[]','page')""")
        connection.execute("""INSERT INTO tools(
            name,version,purpose,status,created_at,updated_at,task_id,manifest,directory,hash
        ) VALUES ('legacy-tool','0.1.0','keep tool','disabled','old','old',9,'{}','legacy','abc')""")
        connection.commit()
        connection.close()

        db = Database(self.path)
        try:
            self.assertEqual(db.connection.execute(
                "SELECT content FROM chat_messages"
            ).fetchall(), [("keep me",)])
            self.assertEqual(db.connection.execute("SELECT content FROM memories").fetchone()[0], "keep this memory")
            self.assertEqual(db.connection.execute("SELECT title FROM goals WHERE id=7").fetchone()[0], "legacy goal")
            self.assertEqual(db.connection.execute("SELECT state FROM tasks WHERE id=9").fetchone()[0], "completed")
            self.assertEqual(db.connection.execute("SELECT title FROM research_sources").fetchone()[0], "Example")
            self.assertEqual(db.connection.execute("SELECT purpose FROM tools").fetchone()[0], "keep tool")
            self.assertIn("worker_log", [row[1] for row in db.connection.execute("PRAGMA table_info(tasks)")])
            self.assertEqual(current_version(db.connection), MIGRATIONS[-1].version)
            self.assertIsNotNone(db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchone())
        finally:
            db.close()

    def test_failed_migration_rolls_back_schema_and_version(self):
        connection = sqlite3.connect(self.path)
        failing = Migration(99, "fails", (
            "CREATE TABLE should_rollback(id INTEGER)",
            "INSERT INTO table_that_does_not_exist VALUES (1)",
        ))
        with patch("lilith.migrations.MIGRATIONS", (failing,)):
            with self.assertRaises(sqlite3.OperationalError):
                migrate(connection, "now")
        self.assertIsNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE name='should_rollback'"
        ).fetchone())
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0], 0)
        connection.close()

    def test_concurrent_openers_do_not_apply_a_migration_twice(self):
        barrier = threading.Barrier(3)
        errors = []

        def open_database():
            try:
                barrier.wait()
                database = Database(self.path)
                database.close()
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=open_database) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(10)
        self.assertFalse(errors)
        connection = sqlite3.connect(self.path)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0], len(MIGRATIONS))
        connection.close()


if __name__ == "__main__":
    unittest.main()
