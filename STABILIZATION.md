# Stabilization pass — 2026-09-24

This pass follows `LILITH_ROADMAP.md`. It adds no major autonomy features and does
not claim that the roadmap's entire definition of done has been met.

## Inspection and baseline

Read the entire `src/lilith` package, all tests (including opt-in live scripts),
README, changelog, roadmap, package configuration, and CI workflow before changing
runtime behavior. Installed Python 3.12.14 and development dependencies in `.venv`.

The unmodified offline suite ran 118 tests: 117 passed and one errored.
`test_desktop_adapters_without_operating_desktop` imported `PIL.Image`, but the
documented development install does not include Pillow. Both CI jobs had the same
dependency gap. The Linux job additionally omitted model arbitration, UI, and
service tests. No live model, public web acceptance script, or owner database was
used for validation.

## Architecture map

| Boundary | Current ownership and findings |
| --- | --- |
| Persistence | `database.py` owns connections; `migrations.py` owns versioned schema. Provenance IDs previously stopped at reflection task inputs and audit text. |
| Durable work | `tasks.py` owns queue transitions; `workers.py` manages process lanes. Recovery trusted a persisted resumable flag and made retry decisions outside the write transaction. |
| Inference | `models.py` owns roles and priority leases. Expiring active leases did not clean abandoned waiting requests on restart; undispatched inbox messages were invisible to arbitration. |
| Conversation | `conversation.py` is shared by console and service; `commands.py` is shared by console and UI. Existing shared logic was preserved. |
| Memory | `memory.py` provides the small retrieval interface. Reflection previously trusted a model-supplied evidence label without checking text or saved message roles. |
| Capabilities | `capabilities.py` owns grants and typed dispatch. Argument auditing ran before type validation and crashed on non-object arguments. |
| Research | `research.py` performs pinned public HTTP retrieval and citation checks. Existing offline SSRF, redirect, byte-limit, quarantine, and citation tests remain in place. |
| Generated tools | `tool_sandbox.py`, `tool_registry.py`, and `workshop.py` retain restricted AST execution, hashes, validation, and durable stage checkpoints. |
| Clients and health | `service.py` owns inbox/scheduling; `home.py` renders the Textual UI. Health counted stopped workers as stale. |

## Bugs and fixes

1. **Reflection evidence could be mislabeled.** A model could claim its own words
   came from the owner. New memory content must occur verbatim in the saved owner
   message; supplied IDs must match the expected roles and exact task text.
   Source IDs are passed by trusted runtime code and persisted with the memory.
   Paraphrased proposals are conservatively rejected. Legacy reflections without
   saved owner IDs do not create memories.
2. **Non-finite reflection numbers became large clamped changes.** NaN/infinity are
   now rejected before confidence, importance, interest, or affect updates.
3. **Abandoned waiting model requests could stall inference after restart.** The
   supervisor clears outstanding requests and leases under the runtime lock before
   recovering tasks, with audit records for each released request.
4. **An inbox message could lose priority before dispatch.** Pending owner inbox
   entries now block background inference, alongside queued conversation tasks.
   Already running inference remains non-preemptive.
5. **Curiosity research received background priority.** Its inference now uses the
   curiosity role, keeping it below reflection and ordinary background work.
6. **Unsafe work could opt into automatic retry.** Enqueue and interruption handling
   both check the broker's read-only capability catalog. Legacy unsafe tasks with
   an incorrect resumable flag go to `needs_review`.
7. **A stale retry decision could overwrite cancellation or completion.** The
   interruption snapshot, decision, and transition now share one write transaction.
   Normal transitions and interruption share the same terminal-update helper.
8. **Canceled parents could enqueue children after cancellation traversal.** Child
   enqueue validates the parent's state and cancellation flag in its transaction.
9. **Malformed capability arguments escaped auditing.** Non-object arguments and
   non-string keys now reach schema rejection with requested/failed audit events.
10. **Health reported stopped workers as stale.** Only running workers with old
    heartbeats are classified as stale.
11. **Offline desktop tests required an undeclared optional package.** Screenshot
    saving now uses a fake adapter, preserving dispatch/storage tests without Pillow.
    Startup installation tests also use temporary data/workspace configuration.
12. **Linux CI missed relevant tests.** Both jobs now discover the complete offline
    suite, including arbitration, Textual UI, and detached service tests using local
    fake model servers.

## Migration and regressions

Migration **5: `memory_message_provenance`** adds nullable `owner_message_id` and
`lilith_message_id` foreign keys to `memories`. Existing rows remain intact with null
provenance; no IDs are guessed or backfilled. Existing migration machinery applies
the change transactionally and once. Reset already clears memories and messages.

New regression coverage checks relabeled assistant text and stage directions,
missing/mismatched source evidence, non-finite updates, provenance persistence and
foreign keys, abandoned model requests, inbox priority before dispatch, unsafe retry
flags including legacy data, late children after cancellation, terminal-state
preservation, malformed capability auditing, and stale-worker health classification.
Existing reflection and migration tests now assert source IDs and legacy nulls.

## Validation

Local validation uses Python 3.12 and the documented commands:

```bash
.venv/bin/python -m compileall src
.venv/bin/python -m ruff check src tests
.venv/bin/python -m unittest discover -s tests -v
```

First-pass local result: **129 tests passed** (11 added), compilation passed, Ruff passed,
and `git diff --check` passed. This includes the Textual UI, console, detached
service, research protections, and workshop tests. No runtime databases, logs,
environments, or credentials are tracked in Git.

Hosted Windows/Linux GitHub Actions have not been executed from this workspace; changing CI configuration
does not establish that hosted CI is green. Live Ollama and actual desktop acceptance
remain separate, opt-in checks.

## Remaining technical debt and deliberately deferred work

- Exact source membership establishes textual provenance, not truth or semantic
  entailment. Excerpts can omit context; model-proposed self-beliefs and interest
  changes still require stronger evidence validation. Existing memories are not
  retroactively authenticated.
- Reflection applies its different updates in separate commits; interruption can
  leave partial updates. It remains non-resumable, so this cannot silently replay.
- The follow-up below addresses live log bounds and known diagnostic secrets.
  Arbitrary private strings without recognizable keys or known environment values
  cannot be classified reliably by a redactor. Research result/error retention and
  total log-directory retention still need further work.
- The follow-up adds worker process identities and cleanup. Legacy rows without
  identities and detached descendants after their root has already exited remain
  limitations. Restart cleanup assumes exclusive runtime ownership; more supervisor
  crash fault injection and OS process-group containment remain future work.
- Cancellation traversal across multiple generations and workshop finalization
  warrant additional adversarial interleaving tests. This pass fixes late direct
  children and atomic retry decisions, not every possible process interleaving.
- Filesystem validation remains subject to check/use races with concurrent external
  filesystem mutation; terminal capabilities remain trusted owner execution.
- SQL read/modify/write paths for identity initialization and interests need a
  separate concurrency pass. Research URL/error redaction and malformed link
  handling, tool-status file/database crash consistency, and sustained log retention
  also deserve dedicated regression coverage.
- Vector retrieval, GitHub SelfDev, and broader hobby systems remain deferred.
  The owner's subsequent explicit shell-access request supersedes the roadmap's
  earlier restriction on generated tools with host permissions.

## Follow-up: owner-authorized shell access

The owner specifically requested unrestricted shell access; the file-writing tool
was only an example and was not created in owner state. Shell access is enabled by
default across entrypoints. Chat routing, executive plans, and generated tools can
use the system shell with the owner's OS permissions and no command/path/network
allowlist. The workspace is the starting directory, not confinement. Setting
`LILITH_ALLOW_SHELL=0` and restarting revokes the grant. Commands still have a
120-second timeout, output bounds, cancellation, and non-resumable task semantics.

Generated tools retain the interpreter and use an explicitly granted `shell` helper.
Their tests use ordered command/result fixtures, never real host operations. A mock
mismatch cannot masquerade as an expected tool error. Shell canaries validate input
and artifacts; final execution performs the host action once. Failing generated
tests reach the existing model correction attempt before an artifact is written.
An empty research query can skip unnecessary web research. Existing pure tools work
unchanged; read-only broker grants cannot invoke shell-capable tools indirectly.

Related worker changes started before this steering were completed and tested:

- Migration **6: `worker_process_identity`** adds nullable worker PID and creation
  time columns without changing existing task data.
- Workers wait on a private launch pipe until the supervisor persists their identity.
  Losing the supervisor before launch approval yields EOF and no execution.
- Restart stops recorded processes only when PID and creation time match, before
  model/task recovery. Identity mismatches never target an unrelated reused PID.
- A pipe reader caps each log at 1 MiB while running, reserves room for exit details,
  drains excess output, and omits oversized lines. Known environment secrets and URL
  credentials/query strings are redacted in logs, audits, and task errors. Model
  response bodies are omitted from malformed-response and HTTP error messages.
- The full suite exposed a concurrent first-open `PRAGMA journal_mode=WAL` BUSY
  race. Initialization now retries only BUSY/LOCKED within the existing 30-second
  budget and closes the connection if initialization fails.

Validation: **143 offline tests passed**, compilation and Ruff passed. Added tests
exercise natural shell routing, executive shell execution, access outside a temporary
workspace, revocation, mock-only tool tests, single real execution, model correction,
log bounds/redaction, launch-gate EOF, and orphan PID matching. All shell effects use
disposable test directories. No live Ollama acceptance or hosted CI run was performed.
