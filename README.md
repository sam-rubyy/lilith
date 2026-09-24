# Lilith v0.1.0

Local persistent chat with background reflection, durable goals, bounded executive
cognition, an audited local capability broker, public-web research, and a persistent
tool workshop. Implements roadmap milestones v0.0.6–v0.1.0. This checkout includes
owner-authorized full shell access for normal requests, executive tasks, and
generated tools.

## Run on Linux

Install Python 3.12 with virtual environment support, then run these commands from
this repository directory:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
./launch-lilith.sh
```

The executable launcher uses this repository's `.venv` and can also be invoked by
its full path from another directory. Alternatively, activate the environment with
`source .venv/bin/activate` and run `lilith`.
Ollama must be running with the configured model installed for conversation.
Closing the dashboard leaves the runtime running; use `/shutdown` to stop it.

## Run on Windows

Use standard Windows CPython 3.12 or newer. From the directory containing this repository:

```powershell
py -3.12 -m venv .venv-windows
.\.venv-windows\Scripts\python.exe -m pip install -e './lilith[desktop]'
.\.venv-windows\Scripts\lilith.exe
```

The desktop extra installs screenshot, keyboard/mouse, and window adapters.
Install just `./lilith` for file, Git, process, terminal, and development capabilities.
Ollama must already be running with the configured model installed for conversation,
reflection, and executive reasoning. File and process jobs do not require a model.

Configuration:

| Variable | Default |
| --- | --- |
| `LILITH_DATA_DIR` | `%LOCALAPPDATA%/Lilith` (or `~/Lilith`) |
| `LILITH_MODEL` | `huihui_ai/qwen3-abliterated:4b` |
| `LILITH_CONVERSATION_MODEL` | `LILITH_MODEL` |
| `LILITH_ROUTER_MODEL` | `LILITH_MODEL` |
| `LILITH_REFLECTION_MODEL` | `LILITH_MODEL` |
| `LILITH_REASONING_MODEL` | `LILITH_MODEL` |
| `LILITH_RESEARCH_MODEL` | `LILITH_MODEL` |
| `LILITH_WORKSHOP_MODEL` | `LILITH_MODEL` |
| `LILITH_KEEP_ALIVE` | `0` (ask Ollama to unload after each request) |
| `LILITH_OLLAMA_URL` | `http://localhost:11434` |
| `LILITH_WORKSPACE` | `<data directory>/workspace` |
| `LILITH_ALLOW_SHELL` | `1`; set `0` and restart to revoke shell access |
| `LILITH_SEARCH_PROVIDER` | `mwmbl`; optional `duckduckgo` HTML adapter |
| `BRAVE_SEARCH_API_KEY` | Optional; selects Brave search when set |
| `LILITH_SEARXNG_URL` | Optional public SearXNG base URL with JSON search enabled; used when no Brave key is set |

Existing SQLite data is preserved by ordered transactional migrations recorded in
`schema_migrations`; upgrades never require deleting the database. Every connection
enables WAL, foreign keys, and a 30-second busy timeout. Stop any older runtime before
launching this version. The runtime and reset utility share an OS lock.

Shell commands run through Bash on Linux or `cmd.exe` on Windows under the owner's
account. There is no command allowlist or filesystem/network confinement; the
workspace is just the starting directory. Normal requests can queue shell work,
and `/run shell.run {"command":"..."}` dispatches a command explicitly. Results
appear in Live activity and Tasks. Shell calls retain a 120-second command timeout,
a 1 MiB capture budget (64 KiB returned), task cancellation, and no automatic replay.
These lifecycle limits do not restrict which commands or paths are accessible.

## A persistent home

Double-click **Launch Lilith.cmd** to open the home. Ask naturally: “Create a tool
that normalizes my journal tags,” “Research this topic,” or “Use that tool on…”.
Conversation context resolves follow-ups such as “No, actually build it.” Tool
requests become saved background workshop jobs, not code pasted into chat. Missing
run inputs or unsupported tool requirements produce a clarification question.

The **Live activity** tab shows research sources, generated implementation, test
reports, and actual outputs. The progress bar counts completed workshop stages;
it is not a time estimate. Research and tool execution use an indeterminate bar
until finished. Background jobs have a separate worker lane from conversation,
while a durable priority arbiter ensures owner conversation enters inference ahead
of queued background cognition on the shared Ollama server.
Saved tools can be reused by asking in chat. Existing open dashboards need to be
closed and reopened to load UI changes; closing a dashboard preserves background work.

`lilith` (also `lilith-home`) opens the Textual dashboard and starts a detached runtime
if needed. Replies stream into the conversation as tokens arrive. Tasks, recorded
intentions, reflection journals, research summaries, self-state, tools, and runtime
health remain visible in the side panel. These are saved activity records, not a
continuous private thought stream. Select a task to inspect its inputs/results,
cancel it, or raise its queued priority. Narrow terminals stack the panels vertically.

Ctrl+L focuses chat, Ctrl+X stops the reply, Ctrl+P pauses/resumes curiosity, and
Ctrl+Q closes the window. Closing the dashboard leaves the runtime alive; `/shutdown`
stops it. Replies and partial interrupted replies persist across client reconnects.
The conversational voice uses existing memories and interests, stays warm and direct,
and preserves the owner's identity/continuity instructions.

The idle scheduler chooses one small public research question from recorded interests
after 15 minutes without an owner message, at most once an hour and six times per UTC
day. It records its intention, queues bounded research, and journals the result.
Owner messages cancel autonomous curiosity work. Pause curiosity stops new sessions
and cancels the current one. Runtime settings are persisted in SQLite and shown in
Health. This is a bounded interest-research loop; broader autonomous projects remain
future work. No model request is needed for an idle heartbeat or dashboard refresh.
Models load for conversation **and** scheduled cognition, then unload by default.

```powershell
lilith-service install-startup  # start hidden at Windows login for this user
lilith-service start
lilith-service status
lilith-service stop
lilith-service remove-startup
```

The service watchdog retries infrastructure crashes up to three times. Interrupted
tasks still follow the recovery policy below. Logs are in `<data>/service.log`.
Login startup requires Windows sign-in and does not run while the computer is asleep
or shut down. Ollama must be available for model work. `lilith-console` retains the
streaming legacy console; stop the service before using that standalone runtime.

## Use background work

Ordinary replies queue reflection immediately. Reflection applies the existing
memory, interest, belief, and affect updates in a separate process, then queues a
journal entry. The input prompt returns without waiting for reflection. Inference uses
audited, crash-recoverable priority leases: conversation and routing outrank owner
work, reflection, background work, and curiosity. Filesystem, Git, HTTP, tests, and
tool execution remain concurrent.

New reflection memories retain exact excerpts from the saved owner message, with
foreign-key links to the owner and assistant message IDs. The runtime checks the
saved roles and text; a model cannot relabel its own response as owner evidence.
These excerpts are owner statements, not independently verified facts. Models must
preserve qualifications and context; excerpt membership alone cannot verify meaning.
Existing memories remain intact with unknown provenance represented by null IDs.
Legacy queued reflections without an owner message ID skip memory creation while
still allowing the other reflection updates. Non-finite numeric updates are rejected.

```text
/tasks
/task 12
/cancel 12
/priority 13 90
/goal owner Inspect the files in my workspace and explain their purpose
/goal shared Explore our project structure
/goal self_directed Investigate the notes in my workspace
/goal maintenance Check the repository status
/goals
/goal-state 1 paused
/goal-state 1 active
/think Compare the approaches discussed in our recent memories
/journal
/health
```

Active goals are selected by descending priority, oldest first. A goal has at most
one active task. Completed goals stop; unsuccessful or interrupted cycles become
`needs_review` instead of looping. `/goal-state <id> active` explicitly starts a new
cycle after review. `/goal-state <id> cancelled` stops it. Goal categories currently
come from owner console commands; spontaneous goal formation is a later milestone.

Executive cycles recall memories, create a plan, independently review/revise it,
authorize each step, execute, verify evidence, and queue a journal. Defaults are six
steps, three model calls, and 180 seconds. Plans receive read-only capabilities plus
`shell.run` when the owner grant is enabled, including shell-capable retained tools.
Shell work has the owner's OS permissions; legacy network/storage budget fields do
not provide confinement for shell commands. Disabling the shell grant restores the
read-only model capability catalog. Unavailable actions are reported for review.

## Request local actions

`/capabilities` displays the exact required/optional argument types. `/run` is the
owner's explicit authorization for that one queued request. Its result is available
through `/task <id>`; the worker records the input, outcome, and error in the audit log.

```text
/run filesystem.create {"path":"notes.txt","content":"Hello"}
/run filesystem.read {"path":"notes.txt"}
/run filesystem.list {"path":"."}
/run filesystem.search {"path":".","pattern":"**/*.txt"}
/run git.status {"working_directory":"my-project"}
/run terminal.run {"command":"python","arguments":["--version"],"working_directory":".","timeout":10,"environment":{},"expected_effect":"Print Python version"}
/run development.venv {"path":"project-env"}
/run development.test {"working_directory":"my-project","directory":"tests","timeout":60}
/run process.list {}
/run desktop.screenshot {"path":"screen.png"}
/run desktop.window_list {}
```

Capabilities cover file reads/writes/create/copy/move/delete/list/search; process
list/inspect/start/stop; structured terminal execution; Git status/diff/log/branch/
checkout/commit/restore; application open/close/restart/focus; screenshots and desktop
click/type/keypress/scroll/window listing; virtual environment creation and unittest
execution. Git commit commits already staged changes. Desktop typing accepts ASCII;
window activation requires a unique title match. Desktop control uses PyAutoGUI's
corner fail-safe. App focus and window enumeration target Windows.

File arguments resolve inside `LILITH_WORKSPACE`, including symlink/junction resolution.
Copy/move only handle files, require unused destinations, and deletion only removes
files or empty directories. Explicit writes can replace existing files. Screenshots
require an unused path. Broker-managed writes have a per-request storage budget.

Terminal commands, development tests, and launched applications run as the owner.
They are trusted code execution, **not an OS sandbox**: their own file/network access
is not confined by broker path validation. The structured cwd and command arguments
are validated, but executing a shell explicitly still executes shell code. Give
these capabilities only to trusted owner requests. Process/app launches intentionally
outlive their launch jobs; bounded terminal/test commands have time/output limits.
Desktop actions report dispatch, not proof of the target application's final state.

## Research agent (v0.0.9)

```text
/research What are Python's rules for numeric division?
/research {"query":"Explain numeric division","urls":["https://docs.python.org/3/tutorial/introduction.html"]}
/task 12
/sources 12
/run browser.search {"query":"Python numeric types"}
/run browser.read {"url":"https://docs.python.org/3/tutorial/introduction.html"}
/run browser.follow_link {"source_id":1,"link_index":0}
/run browser.download {"url":"https://example.com/public-resource.zip"}
```

Research searches, retrieves up to five pages, selects bounded relevant excerpts,
and asks the local model for a cited summary. Every claim must reference IDs from
sources actually retrieved in that task. `/sources` exposes the source URL, title,
retrieval timestamp, byte count, and SHA-256. Summary results and limitations appear
in `/task`; the journal retains the summary. Citation ID validation prevents invented
references, but factual interpretation still depends on the model and source quality.

The default provider is [Mwmbl](https://github.com/mwmbl/mwmbl), whose index is smaller
than commercial search engines. Optional adapters follow the
[Brave API](https://api-dashboard.search.brave.com/api-reference/web/search/get) and
[SearXNG JSON API](https://docs.searxng.org/dev/search_api.html). Provider errors and
challenges are surfaced for review; no CAPTCHA/login bypass is attempted. Supplying
URLs skips search. Queries are sent to the configured public search provider. The
local model is instructed to omit sample data and owner details from generated
queries; `research_query` lets the owner specify the exact query. Do not put private
data in research questions. Supplying URLs avoids generated search queries entirely.

The fetcher only performs GET requests to public HTTP(S) addresses on ports 80/443.
DNS is resolved and checked before connecting to a pinned address; each redirect is
revalidated. It sends no browser cookies, follows no login flows, and executes no
JavaScript. Credentials used by a search provider stay on that origin. Private IPs,
URL credentials, HTTPS downgrades, and compressed responses are rejected. Defaults:
12 requests (including redirects), 4 MiB total, 1 MiB per response, 90 seconds of
network work. Research workers have a separate 240-second overall deadline.

HTML and text/JSON/XML documentation are readable; PDF rendering, authenticated
sites, and JavaScript-only pages are not supported by this reader. Binary downloads
are stored as `<data directory>/quarantine/<task-id>/<sha256>.bin`, never automatically
opened, unpacked, imported, or executed. Quarantine is outside the default workspace.

## Tool workshop (v0.1.0)

Submit an owner request with concrete JSON input and, preferably, an expected result:

```text
/build-tool {"request":"Convert celsius to fahrenheit using celsius * 9 / 5 + 32; return an object with fahrenheit","data":{"celsius":0},"expected":{"fahrenheit":32},"urls":["https://docs.python.org/3/tutorial/introduction.html"]}
/tasks
/task 20
/tools
/tool <generated-name>
/tool-run <generated-name> {"celsius":100}
/tool-state <generated-name> approved
/tool-state <generated-name> disabled
```

`urls` and `research_query` are optional. The model can skip research with an empty
query when no external reference is needed; supplied URLs or a query still request
research. The workflow first checks granted capabilities and registered tools.
Requests to build reusable tools are routed to generation or tool reuse. It creates separate task
records for research, design, implementation, tests, capability review, canary,
execution, and verification. `/task` on the parent shows its current child and stage
history. Cancel the parent to cancel active/queued children. A workshop is limited
to nine stages and 30 minutes. Each workshop model role gets at most one correction
attempt for malformed output or failed generated tests; corrections and proposals
are retained for inspection.

Generated artifacts live at `<data directory>/tools/experimental/<generated-name>/`:

```text
manifest.json
tool.py
tests/cases.json
README.md
```

Manifests record purpose, version, author, schemas, permissions, resource limits,
timeout, rollback strategy, hash, and status. Source is canonically formatted, checked
against the allowed syntax/names, tested with input/output schema enforcement, and
independently reviewed by the model. At least three tests must cover successful and
failure cases. Only passing, reviewed tools become experimental. Canary and final
verification failures disable newly created tools. An owner's `expected` result is
checked independently of the model's judgment.

Generated tools keep the Python AST interpreter and JSON input/output interface.
Pure tools use `permissions: []`. Tools with `permissions: ["shell"]` can call
`shell(command)`, which returns `{"returncode": ..., "output": ...}`. This helper
can run arbitrary programs, scripts, imports via an external Python process, and
filesystem/network operations under the owner's account. The runtime checks the
owner's grant at invocation and on each shell call; a model cannot restore a
revoked grant.

AST computation remains metered: 20,000 operations, 2 seconds excluding shell wait,
64 KiB JSON, 1,000 items per collection, nesting depth 20, and 256-bit integers.
Generated tests use ordered `shell_calls` fixtures and never execute real commands.
For shell tools the canary validates inputs and integrity; only the execution stage
performs the real action, avoiding duplicate side effects. This is host execution,
not an OS sandbox. Contracts are in `src/lilith/tool_sandbox.py`.

The schema subset supports explicit types, object properties/required keys, arrays,
enums/constants, numeric bounds, and length limits. Remote references and regex
schemas are not accepted. Shell-capable tools can use host programs for tasks such
as file processing or package installation.

Source, manifest, README, and tests are hash-checked before every invocation. A
modified artifact or mismatch with the database fails closed. The registry survives
restart. `/tool` shows its contract and test/review evidence. Retained object-input
tools are also available through `tool.invoke` in the capability broker and to the
executive planner; `/tool-run` accepts any JSON input matching the tool's schema.

## Recovery and operation

One supervisor thread manages six bounded spawned-process lanes: conversation,
reflection, journal, executive/capability work, research, and tool development/curiosity.
Each worker opens its own SQLite connection. Failed workers write bounded diagnostic
logs under `<data>/logs/tasks/`, and task inspection includes the corresponding path.
WAL, a busy timeout, and `BEGIN IMMEDIATE` transactions serialize queue/goal writes.
Claims, task transitions, and associated audit events commit atomically. Worker
heartbeats are persisted by the supervisor while each process is alive.

Cancellation terminates active workers and their descendant commands. Shutdown waits
for worker cleanup before closing the database. Queued tasks survive shutdown.
Interrupted read-only tasks can retry within their attempt limit; any task that may
have produced side effects becomes `needs_review`. Reflection and journaling are
conservatively non-resumable because they update persistent state. Inspect the audit
and result before submitting replacement work; no blind replay of interrupted writes.

Retry eligibility is checked against the capability broker's read-only catalog at
enqueue and recovery, including legacy tasks with incorrect resumable flags.
Restart cleanup clears abandoned model requests and leases while holding the
runtime lock. Owner messages block background inference as soon as they reach the
inbox, before dispatch creates a conversation task.

Waiting workshop parents persist their child/checkpoint atomically and can continue
after restart when the child was safely queued or completed. Interrupted active
stages require review. A failed stage prevents subsequent stages from starting.

The reset utility clears task/goal, source, and tool-registry state as well as existing memories. It
refuses to run alongside this runtime and uses SQLite's online backup API to include
WAL contents. Artifact and quarantine files remain on disk but lose their registry
entries, so old tools cannot silently reactivate. It remains destructive and is never invoked by normal startup.

## Validation

From this repository directory, with the environment activated:

```powershell
python -m unittest discover -s tests -v
```

GitHub Actions is configured to run this complete offline suite on Windows and Linux
with Python 3.12. Desktop adapters are mocked without optional desktop dependencies;
service tests use temporary state and local fake model servers. Both
jobs compile the package and run Ruff's correctness-oriented static checks. Install
the local development tools with `python -m pip install -e ".[dev]"`.

Tests use temporary databases/workspaces, a fake model, and real spawned workers.
They do not modify the owner's live memories or operate the real mouse/keyboard.

See [the stabilization report](STABILIZATION.md) for the baseline failure, fixes,
regression coverage, and remaining work from `LILITH_ROADMAP.md`.

An explicit live acceptance run uses temporary state, public documentation, the
configured search provider, and the configured Ollama model:

```powershell
python tests/live_acceptance.py
```

It builds a Celsius/Fahrenheit transformation through the full workflow, checks its
output, and reuses the retained tool with another input. This test is not part of
offline unittest discovery. Structured model responses use Ollama's
[schema-based output format](https://docs.ollama.com/capabilities/structured-outputs).
