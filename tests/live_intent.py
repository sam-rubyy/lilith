"""Opt-in routing smoke test with the configured local model and disposable state."""
from pathlib import Path
import tempfile

from lilith.config import get_model_name
from lilith.database import Database
from lilith.intent import route_request
from lilith.model_gateway import OllamaGateway
from lilith.tasks import TaskStore


with tempfile.TemporaryDirectory(prefix="lilith-routing-") as directory:
    db = Database(Path(directory) / "state.db")
    try:
        store = TaskStore(db)
        db.save_message("assistant", "We could create a tool that converts Celsius to Fahrenheit.")
        reply = route_request(db, store, OllamaGateway(get_model_name()), "Yes, actually build that tool and test it.")
        assert len(store.rows("SELECT id FROM tasks WHERE type='workshop'")) == 1, reply
        print(reply, flush=True)
        print("Live contextual routing passed; disposable job was not executed.", flush=True)
    finally:
        db.close()
