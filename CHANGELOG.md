# v0.1.0 — 2026-09-23

Includes roadmap milestones v0.0.9 and v0.1.0.

- Added canonical Windows and portable Linux CI on Python 3.12, including package
  installation, bytecode compilation, offline tests, and Ruff correctness checks.
- Added transactional schema migrations, foreign-key enforcement, provenance-safe
  reflection, prioritized model leases, role-specific model configuration, a chat
  routing fast path with latency telemetry, bounded worker crash logs, outcome-based
  curiosity reinforcement, shared memory retrieval, and expanded health diagnostics.

- Streaming replies in the console and a new default Textual home with chat, task
  inspection/cancellation/priorities, journals, self-state, tools, and health.
- Durable chat inbox and detached runtime: closing the client does not stop work.
  Hidden Windows login startup, bounded crash recovery, and explicit shutdown.
- Warm, memory-grounded personality preserving the owner's base identity prompt.
- Model loading on demand, unloading after requests, and bounded idle curiosity
  that records intentions and researches interests; owner messages preempt it.

- Public-web research with Mwmbl search by default, optional Brave/SearXNG/DuckDuckGo
  adapters, HTML/text retrieval, link following, and local cited summaries.
- Persisted source URLs, timestamps, hashes, and text excerpts. Downloaded binary
  resources remain quarantined under hash filenames and are never executed.
- Public-address checks, DNS-pinned connections, redirect revalidation, network/time
  limits, and source-ID validation for every research-summary claim.
- Persistent tool workflows with separate capability-gap, research, design,
  implementation, testing, review, canary, execution, and verification tasks.
- Generated manifests, source, test cases, README files, capability review, bounded
  canaries, and experimental registration; existing tools/capabilities are reused.
- A metered interpreter for a pure JSON subset of Python, with no generated-code
  exec/eval, imports, host access, or ambient capabilities. General Python package
  execution is outside this sandbox's scope.
- Hash verification at invocation, typed input/output contracts, tool lifecycle
  commands, and retained tool access from the executive planner.
- Schema-constrained model responses, bounded research context, and at most one
  model correction per workshop role. Failures stop the workflow for review.
- Cascading workflow cancellation, atomic stage checkpoints, and restart recovery
  without blind replay of interrupted stages. Adds research and workshop worker lanes.
- Offline tests plus an opt-in live acceptance script use disposable state.

# v0.0.8 — 2026-09-23

Includes roadmap milestones v0.0.6, v0.0.7, and v0.0.8.

- Conversation queues reflection instead of waiting for it. Reflection and journals
  run in separate bounded processes with their own SQLite connections.
- Tasks persist across restarts, have priorities, cancellation, heartbeats, attempt
  limits, deadlines, results, parent/goal links, and audited state transitions.
- Active goals select work by priority. Planner, reviewer, and verifier roles run
  within a bounded executive task and retain actual capability results as evidence.
- The capability broker exposes structured local file, Git, process, terminal,
  application, desktop, screenshot, virtual environment, and test operations.
- Owner console commands expose tasks, goals, journaling, health, and capability
  invocation. Model-generated plans receive read-only local capabilities.
- Interrupted work with possible side effects requires review. The runtime prevents
  duplicate supervisors; shutdown cleans up active workers and bounded commands.
- Reset tooling includes task/goal data, excludes a running runtime, and backs up
  SQLite through its online backup API, preserving WAL contents.
- Tests cover real worker processes, blocked-reflection chat responsiveness,
  cancellation/recovery, goal execution, capability auditing, path checks, and mocked
  desktop adapters. Live Ollama quality and real desktop interactions require local
  acceptance testing.
