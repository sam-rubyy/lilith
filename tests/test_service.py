"""Real detached processes with a deterministic local streaming model server."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from lilith.database import Database
from lilith.service import RuntimeState, ensure_service, _SERVICE_PROCESSES


class ServiceTests(unittest.TestCase):
    def test_legacy_runtime_conflict_is_explained_without_spawning(self):
        from lilith.workers import RuntimeLock
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / 'lilith.db')
            lock = RuntimeLock(db.path)
            try:
                with patch('lilith.service.subprocess.Popen') as launch:
                    with self.assertRaisesRegex(RuntimeError, '/quit'):
                        ensure_service(db)
                    launch.assert_not_called()
            finally:
                lock.close()
                db.close()

    def test_detached_stream_survives_client_close_and_stops_cleanly(self):
        calls = []
        release = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(payload)
                self.send_response(200)
                self.end_headers()
                if payload.get('stream'):
                    self.wfile.write(b'{"message":{"content":"Hello"},"done":false}\n')
                    self.wfile.flush()
                    release.wait(10)
                    self.wfile.write(b'{"message":{"content":" there."},"done":true}\n')
                else:
                    content = json.dumps({'action': 'chat'}) if 'action' in payload.get('format', {}).get('properties', {}) else '{}'
                    self.wfile.write(json.dumps({'message': {'content': content}, 'done': True}).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            'LILITH_DATA_DIR': temp, 'LILITH_WORKSPACE': str(Path(temp) / 'workspace'),
            'LILITH_MODEL': 'fake', 'LILITH_OLLAMA_URL': f'http://127.0.0.1:{server.server_port}'}):
            db = Database(Path(temp) / 'lilith.db')
            state = RuntimeState(db)
            try:
                first = ensure_service(db)
                self.assertTrue(first['alive'])
                self.assertEqual(ensure_service(db)['pid'], first['pid'])
                self.assertEqual(calls, [])  # Idle service never loads the model.
                ident = state.submit('Hello')
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    row = state.store.rows('SELECT * FROM inbox WHERE id=?', (ident,))[0]
                    if row['response']:
                        break
                    time.sleep(.05)
                self.assertEqual(row['response'], 'Hello')
                self.assertEqual(row['state'], 'streaming')
                db.close()  # Closing the client does not own the service lifetime.
                db = Database(Path(temp) / 'lilith.db')
                state = RuntimeState(db)
                self.assertTrue(state.status()['alive'])
                release.set()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    row = state.store.rows('SELECT * FROM inbox WHERE id=?', (ident,))[0]
                    if row['state'] == 'completed':
                        break
                    time.sleep(.05)
                self.assertEqual(row['response'], 'Hello there.')
                self.assertEqual(row['state'], 'completed')
                self.assertTrue(all(call['keep_alive'] == '0' for call in calls))
            finally:
                release.set()
                with state.store.transaction() as c:
                    c.execute('UPDATE runtime_status SET stop_requested=1')
                for process in _SERVICE_PROCESSES:
                    process.wait(timeout=15)
                self.assertFalse(state.status()['alive'])
                db.close()
                server.shutdown()
                server.server_close()

    def test_startup_uses_hidden_python_and_explicit_configuration(self):
        from lilith.startup import install, remove
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'Lilith.vbs'
            with patch('lilith.startup.startup_path', return_value=target):
                self.assertEqual(install(), target)
                content = target.read_text(encoding='utf-16')
                self.assertIn('lilith.service start', content)
                self.assertIn('0, False', content)
                self.assertIn('LILITH_DATA_DIR', content)
                remove()
                self.assertFalse(target.exists())
