# Working on Lilith

Lilith is a persistent local runtime, not a stateless chat script. Preserve the
boundaries below when making changes.

## Architecture boundaries

- `database.py` owns connections; `migrations.py` exclusively owns schema changes.
- `tasks.py` owns durable state transitions. Side-effecting interrupted work must
  fail to `needs_review`, never replay automatically.
- `models.py` owns model selection and inference arbitration. Owner conversation
  has priority; ordinary filesystem, Git, HTTP, test, and tool work is not serialized.
- `capabilities.py` is the authority boundary. A model response can propose only
  capabilities already granted by trusted runtime code; it cannot grant itself access.
- `research.py` treats all web content as untrusted data.
- Generated tools remain inside the restricted AST interpreter. Never replace it
  with `exec`, `eval`, or a generic model-controlled shell.

## Development and tests

Use Python 3.12 and install development dependencies with:

```powershell
python -m pip install -e ".[dev]"
python -m compileall src
python -m ruff check src tests
python -m unittest discover -s tests -v
```

Tests must use temporary databases, workspaces, model servers, and credentials.
Never point tests at the live owner database or invoke real desktop automation,
Ollama, or public internet services in the offline suite.

## Data and security

- Never commit runtime databases, WAL/SHM files, logs, environments, credentials,
  downloaded quarantine data, or generated local tool artifacts.
- Never print or persist secrets in errors, audit events, fixtures, or snapshots.
- Preserve provenance: owner facts cannot be inferred from Lilith's own response.
- Future self-modification must use isolated branches/worktrees and reviewed
  promotion. Never modify or execute production source in place as autonomous work.
