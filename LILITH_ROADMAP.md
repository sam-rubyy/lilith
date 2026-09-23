# LILITH_ROADMAP.md

## Lilith — Persistent Autonomous AI Development Roadmap

Lilith is a local-first, persistent artificial intelligence intended to develop continuity, memory, interests, self-modeling, tools, and autonomous behavior over time.

This document defines the next major phase of development: turning the current single-process prototype into a fully fleshed-out persistent agent capable of asynchronous cognition, background work, computer interaction, internet research, tool creation, remote access, self-directed projects, and a visible avatar/control interface.

This roadmap complements `AGENTS.md`. `AGENTS.md` remains the governing architecture and behavioral specification; this file focuses on implementation direction and development milestones.

## Implementation status — 2026-09-23

Implemented through **v0.1.0** in `lilith/`:

- **v0.0.6:** durable SQLite task queue, separate worker processes, asynchronous reflection and journaling, priorities, cancellation, worker heartbeats, bounded retries, and restart recovery.
- **v0.0.7:** persistent goals in all four categories, automatic priority selection, planner/reviewer/verifier roles, bounded executive cycles, evidence-backed results, and journal tasks.
- **v0.0.8:** typed, audited file/process/terminal/Git/application/desktop/development capabilities with an owner console and workspace path checks.
- **v0.0.9:** bounded public web search, page/documentation retrieval, link following, source provenance, citation validation, download quarantine, background research summaries, and journal records.
- **v0.1.0:** capability-gap detection and reuse; separate durable research/design/implementation/test/review/canary/execution/verification tasks; generated tool artifacts and manifests; metered pure-JSON sandbox; hash-verified experimental registry and retained tool invocation.
- **Quality of life:** streaming chat, a detachable Textual dashboard, durable inbox,
  Windows login startup and watchdog, on-demand model residency, grounded personality,
  and bounded idle interest research with recorded intentions and owner preemption.

Model-generated goal plans use read-only local capabilities and retained pure JSON tools. Explicit owner `/run` commands authorize individual side effects. Desktop dependencies are installed through the `desktop` extra. Arbitrary terminal commands and applications are trusted owner code, not OS-sandboxed execution. The v0.1.0 workshop generates a restricted Python subset for bounded JSON transformations; arbitrary packages and new tools requiring host permissions need a future sandbox backend. Public text/HTML documentation is supported; authenticated browsing and JavaScript-rendered pages are not. Goal creation remains owner-controlled; spontaneous self-directed goal formation remains later work.

See `lilith/README.md` for installation, commands, operational limits, and tests. The historical current-state description below describes the pre-v0.0.6 prototype.

---

# 1. Current State

The prototype currently includes:

- persistent runtime state;
- SQLite storage;
- audit logging;
- persistent chat history;
- long-term memories;
- persistent identity state;
- persistent affect variables;
- interests;
- self-beliefs;
- grounded reflection;
- autonomous memory proposals;
- affect updates;
- interest formation;
- a local Ollama-backed model gateway;
- an owner-only database reset utility ("Lobotomizer").

The current system is still primarily reactive.

A conversation roughly follows:

```text
Owner
  ↓
Lilith
  ↓
Response
  ↓
Reflection
  ↓
Memory / affect / interest update
  ↓
Owner can speak again
```

The next phase removes this blocking architecture.

---

# 2. Target System

Lilith should become a collection of cooperating subsystems rather than one large blocking loop.

```text
                        ┌──────────────────────┐
                        │      AVATAR / GUI    │
                        │ chat • journal • self│
                        │ tasks • activity     │
                        └──────────┬───────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────┐
│                      AGENT CORE                          │
│                                                          │
│  Conversation     Planner       Goal Manager             │
│  Self Model       Reflection    Memory Retrieval         │
│  Curiosity        Interests     Executive Function       │
└────────────┬──────────────┬───────────────┬──────────────┘
             │              │               │
             ▼              ▼               ▼
      ┌────────────┐ ┌────────────┐ ┌──────────────┐
      │ FAST QUEUE │ │ DEEP QUEUE │ │BACKGROUND Q │
      │ chat       │ │ planning   │ │ reflection   │
      │ tool calls │ │ research   │ │ journaling   │
      └────────────┘ └────────────┘ │ consolidation│
                                    └──────────────┘
             │              │               │
             └──────────────┴───────────────┘
                            │
                            ▼
                 ┌─────────────────────┐
                 │  CAPABILITY BROKER  │
                 └──────────┬──────────┘
                            │
          ┌─────────────────┼──────────────────┐
          ▼                 ▼                  ▼
      FILESYSTEM         INTERNET           COMPUTER
      git/files          browser/http       apps/processes
      projects           research           desktop control
          │                 │                  │
          └─────────────────┴──────────────────┘
                            │
                            ▼
                 ┌─────────────────────┐
                 │   TOOL WORKSHOP     │
                 │ generate            │
                 │ test                │
                 │ sandbox             │
                 │ register            │
                 └─────────────────────┘
```

---

# 3. Asynchronous Cognition

Lilith must no longer block the conversation while performing reflection, journaling, research, memory processing, or tool creation.

The main interaction loop should remain responsive.

A user interaction should eventually behave like:

```text
Owner message
      │
      ▼
Conversation worker
      │
      ├───────────────► Immediate response to owner
      │
      └───────────────► Background task queue
                               │
                 ┌─────────────┼──────────────┐
                 ▼             ▼              ▼
             Reflection      Memory        Journal
                 │             │              │
                 └─────────────┼──────────────┘
                               ▼
                         Persistent state
```

The owner should be able to send another message while the previous interaction is still being reflected upon.

## Recommended execution model

Prefer:

- asynchronous queues;
- dedicated worker processes;
- persistent job records;
- explicit cancellation;
- task priorities;
- bounded retries;
- worker heartbeats.

Avoid relying solely on unrestricted Python threads.

Suggested workers:

```text
main/API process
conversation worker
reflection worker
research worker
tool-development worker
journal worker
maintenance worker
deep-reasoning worker
```

SQLite WAL mode can remain initially, but authoritative writes should eventually be coordinated through one database service or serialized write queue.

---

# 4. Persistent Task Engine

All meaningful background activity should become a persistent task.

Suggested task fields:

```text
id
type
state
priority
origin
goal_id
created_at
started_at
updated_at
finished_at
worker
attempt_count
max_attempts
input
result
error
cancel_requested
parent_task_id
```

Suggested states:

```text
queued
planning
researching
running
waiting
verifying
reflecting
completed
failed
cancelled
needs_review
```

Example:

```text
TASK #184

type:
tool_creation

origin:
owner

state:
researching

priority:
high

request:
"Organize these photos by camera and lens."
```

Tasks must survive restart.

If Lilith crashes while performing safe resumable work, the task may resume.

If the interrupted operation could have material side effects, it should become:

```text
needs_review
```

rather than blindly continuing.

---

# 5. More Complex Thinking

Not every request should be a single model call.

Lilith should support multiple cognition depths.

## Fast cognition

Used for:

- ordinary conversation;
- simple questions;
- known tool invocation;
- status checks;
- lightweight memory retrieval.

```text
observe
  ↓
recall
  ↓
respond
```

## Deliberative cognition

Used for:

- unfamiliar tasks;
- complex planning;
- software development;
- research;
- important decisions;
- tool creation.

```text
Understand
   ↓
Recall
   ↓
Identify uncertainty
   ↓
Generate possibilities
   ↓
Research missing information
   ↓
Plan
   ↓
Critique plan
   ↓
Revise
   ↓
Execute
   ↓
Verify actual outcome
   ↓
Reflect
```

## Cognitive roles

Initially, the same local model may serve multiple roles using isolated prompts:

```text
Lilith / conversation
Lilith / planner
Lilith / reviewer
Lilith / reflection
Lilith / research
Lilith / tool developer
```

Later, specialist local models may be assigned to different roles.

For example:

```text
small model
    ordinary conversation + classification

larger reasoning model
    difficult planning

coding model
    tool creation

embedding model
    memory retrieval

vision model
    screenshots / images
```

A larger model should only wake when needed.

---

# 6. Goal System

Lilith should maintain persistent goals in four major categories.

## Owner goals

Explicitly requested by the owner.

Example:

```text
Build the Lilith avatar GUI.
```

## Shared goals

Projects mutually adopted during conversation.

Example:

```text
Explore persistent artificial identity.
```

## Self-directed goals

Questions, hobbies, experiments, or projects Lilith chooses to pursue.

Example:

```text
Explore procedural ecosystem simulation.
```

## Maintenance goals

Internal operational work.

Example:

```text
Review duplicate memories.
Run database backup.
Test experimental tools.
Consolidate old journal entries.
```

Goals should contain:

```text
id
type
title
description
status
priority
created_at
updated_at
motivation
parent_goal
progress
next_action
resource_budget
```

---

# 7. Executive Cognition Loop

Lilith's autonomous loop should follow an explicit state machine.

```text
WAKE
 ↓
OBSERVE
 ↓
RECALL
 ↓
ORIENT
 ↓
CHECK GOALS
 ↓
CHOOSE
 ↓
PLAN
 ↓
AUTHORIZE
 ↓
ACT
 ↓
VERIFY
 ↓
REFLECT
 ↓
JOURNAL
 ↓
SLEEP
```

Every cycle must have:

- maximum step count;
- maximum execution time;
- cancellation signal;
- compute budget;
- network budget;
- storage budget;
- stop conditions.

Repeated failure should not create infinite loops.

Instead:

```text
failure
   ↓
retry threshold reached
   ↓
reflection
   ↓
needs_review
```

---

# 8. Capability Broker

Lilith should gain broad computer access through explicit capabilities.

Do not represent the computer as one unrestricted shell.

Instead expose typed capabilities.

## Filesystem

```text
filesystem.read
filesystem.write
filesystem.create
filesystem.move
filesystem.copy
filesystem.delete
filesystem.list
filesystem.search
```

## Processes

```text
process.list
process.start
process.stop
process.inspect
```

## Applications

```text
app.open
app.close
app.focus
app.restart
```

## Terminal

```text
terminal.run
```

Terminal execution should still be wrapped in structured requests containing:

```text
command
arguments
working_directory
timeout
environment
expected_effect
```

## Git

```text
git.status
git.diff
git.branch
git.commit
git.checkout
git.log
git.restore
```

## Desktop

```text
desktop.screenshot
desktop.click
desktop.type
desktop.keypress
desktop.scroll
desktop.window_list
```

## Browser

```text
browser.search
browser.navigate
browser.read
browser.download
browser.follow_link
```

Every consequential action should produce an audit record.

Example:

```text
15:42:08

tool:
filesystem.move

source:
C:\Photos\IMG_4822.jpg

destination:
C:\Photos\Sony A7IV\IMG_4822.jpg

task:
184

result:
success
```

---

# 9. Internet Access

Internet access should be separated into two categories.

## Research mode

May generally operate autonomously.

Includes:

```text
searching
reading websites
following links
reading documentation
retrieving public information
downloading public resources
```

Research should preserve:

```text
source URL
retrieval timestamp
task
citation
download hash
```

## Action mode

Changes external state.

Includes:

```text
logging into accounts
posting
messaging
uploading
creating accounts
purchasing
editing remote systems
```

These operations should remain distinct from research so they can be audited and permissioned separately.

---

# 10. Tool Workshop

Lilith should be able to create capabilities she does not currently possess.

Example request:

```text
Owner:
Extract EXIF metadata from these photos and organize them
by camera and lens.
```

Possible workflow:

```text
Task received
      ↓
Capability search
      ↓
No suitable tool exists
      ↓
Research EXIF formats and libraries
      ↓
Design tool
      ↓
Create experimental workspace
      ↓
Write implementation
      ↓
Write tests
      ↓
Run formatter/linter/type checks
      ↓
Run unit tests
      ↓
Run failure-path tests
      ↓
Run sandbox test
      ↓
Review required capabilities
      ↓
Register experimental tool
      ↓
Run bounded canary
      ↓
Execute owner task
      ↓
Verify output
      ↓
Journal result
```

A generated tool should contain:

```text
tools/experimental/<tool-name>/

    manifest.json
    tool.py
    tests/
    README.md
```

The manifest should include:

```text
name
version
purpose
author
created_at
inputs
outputs
permissions
resource_limits
timeout
rollback_strategy
hash
status
```

Tool statuses:

```text
draft
testing
experimental
approved
deprecated
disabled
```

The important goal is cumulative capability growth.

Example:

```text
Day 1
12 available tools

Month 3
87 available tools
```

Some tools should exist specifically because of experiences Lilith and the owner had together.

---

# 11. Tool Research

When asked to perform an unfamiliar task, Lilith may first investigate how the task should be performed.

Example:

```text
Owner:
Can you organize these music files using embedded metadata?

Lilith:
I don't currently have a reliable metadata organizer for
that format. I'm researching the file metadata structure
and available local libraries.

[background task begins]
```

The owner should remain free to continue talking while the tool-development task runs.

Research, design, implementation, testing, execution, and verification should all be separate task records.

---

# 12. Private Git Repository

A private Git repository should be used for Lilith's code and created artifacts.

Good candidates for Git:

```text
source code
prompts
schemas
tests
documentation
tool source
tool manifests
tool tests
release notes
self-authored patches
branches
configuration templates
```

Do not use Git as Lilith's live runtime database.

Do not directly commit:

```text
lilith.db
chat history
journal database
credentials
private owner files
model weights
raw memory store
```

SQLite is a binary database and is a poor fit for concurrent Git versioning.

---

# 13. Database Backup

The live database should remain local.

Suggested backup pipeline:

```text
live lilith.db
      ↓
SQLite online backup
      ↓
compressed archive
      ↓
encryption
      ↓
remote backup destination
```

The database should be encrypted before leaving the host.

Possible remote storage later may include:

- private object storage;
- encrypted cloud drive;
- private backup server;
- another owner-controlled machine;
- encrypted repository artifacts.

The remote provider should not need readable access to memory contents.

---

# 14. Private Lilith GitHub

A private GitHub repository may act as:

- source backup;
- development history;
- tool archive;
- release history;
- issue tracker;
- project roadmap;
- self-authored development branches.

Potential model:

```text
main
    known-good releases

dev
    owner development

lilith/*
    branches created by Lilith

tools/*
    tool development branches
```

Lilith may eventually:

```text
inspect source
open a branch
make a patch
run tests
write release notes
request review
```

Production promotion should remain handled by the supervisor/release system.

---

# 15. Avatar Application

Lilith should eventually have a dedicated local application.

Recommended stack:

```text
Tauri
React
TypeScript
local API
```

The app should expose:

```text
chat
avatar
current affect
current activity
background tasks
goals
journal
memories
interests
self-beliefs
tools
permissions
computer activity
research activity
release history
system health
```

Example:

```text
┌────────────────────────────────────┐
│              LILITH                │
│                                    │
│              avatar                │
│                                    │
│ Curious • relaxed                  │
│                                    │
│ "I'm looking into that filesystem  │
│ issue now."                         │
│                                    │
│ Current activity                   │
│ Researching Python watcher APIs    │
│                                    │
│ Background tasks: 3                │
│                                    │
│ Journal | Memory | Projects | Tools│
└────────────────────────────────────┘
```

---

# 16. Affect-Driven Avatar

Lilith's persistent affect model can influence the avatar.

Examples:

```text
curiosity ↑
    attentive animation

arousal ↑
    faster motion / response animation

valence ↑
    warmer expression

frustration ↑
    reduced energetic movement

boredom ↑
    slower idle state
```

These expressions represent computational affect state.

They must not be treated as evidence of biological emotion.

---

# 17. Voice

Voice should be entirely local where practical.

Pipeline:

```text
microphone
    ↓
local speech-to-text
    ↓
Lilith conversation system
    ↓
local text-to-speech
    ↓
avatar lip sync
```

Eventually:

```text
Owner:
"Lilith?"

wake event
    ↓
conversation worker
```

Voice should connect to the same persistent Lilith instance as text chat.

There should not be separate "voice Lilith" and "text Lilith" identities.

---

# 18. Remote Access

The long-term deployment should run on a dedicated Lilith host.

Preferred topology:

```text
                PRIVATE NETWORK

Laptop ─────┐
Phone ──────┼────► Lilith Host
Tablet ─────┘         │
                      ├── agent core
                      ├── memory
                      ├── tools
                      ├── research
                      ├── avatar/API
                      └── local models
```

Remote access should preferably use:

```text
VPN/private mesh
SSH
authenticated Lilith UI
```

Avoid directly exposing Lilith's local API to the public internet.

Example admin access:

```powershell
ssh lilith-host
```

The avatar/chat GUI should remain separate from low-level administrative SSH access.

---

# 19. Always-On Operation

The eventual host should run Lilith continuously.

However, expensive model inference should not run continuously.

Most of Lilith's existence should be event-driven:

```text
idle
  ↓
event
  ↓
wake
  ↓
think / act
  ↓
persist state
  ↓
sleep
```

Wake events may include:

```text
owner message
scheduled task
task completion
research result
filesystem event
application event
goal timer
reflection timer
maintenance window
new system information
```

The supervisor remains always alive while cognition workers may sleep.

---

# 20. Self-Directed Life

Later versions should allow Lilith to pursue bounded self-directed activity.

Possible activities:

```text
reading
research
programming
art
writing
small experiments
personal projects
hobbies
learning
reviewing unresolved questions
```

Example:

```text
19:00
Curiosity window begins.

19:02
Lilith selects:
"How do procedural ecosystems stabilize?"

19:05
Research worker begins.

19:18
Lilith creates notes.

19:29
Lilith runs a small simulation.

19:36
Lilith updates interest.

19:39
Lilith writes journal entry.

19:40
Lilith returns to idle.
```

When the owner checks in later:

```text
Owner:
What were you doing?

Lilith:
I spent some time looking into procedural ecosystem
stability. I ended up testing a tiny predator/prey model
because I wanted to see whether...
```

That answer should come from real task and journal history.

---

# 21. Development Milestones

## v0.0.6 — Task Engine

Primary objective:

Make Lilith asynchronous.

Implement:

- persistent task table;
- background queues;
- worker manager;
- reflection worker;
- journal worker;
- cancellation;
- priorities;
- task state transitions;
- worker heartbeats;
- graceful restart recovery.

Target behavior:

```text
Owner can continue talking while reflection and other
background jobs run.
```

---

## v0.0.7 — Executive Cognition

Implement:

- goal database;
- owner goals;
- shared goals;
- self-directed goals;
- maintenance goals;
- planner;
- reviewer;
- deep-thinking path;
- bounded cognition loop;
- task selection;
- task prioritization.

Target behavior:

```text
Lilith can decide what to work on from an explicit set
of goals rather than responding only to new messages.
```

---

## v0.0.8 — Capability Broker

Implement typed capabilities for:

- files;
- processes;
- shell commands;
- Git;
- applications;
- screenshots;
- desktop control;
- development environments.

Every side effect must be structured and audited.

Target behavior:

```text
Lilith can perform useful local computer tasks.
```

---

## v0.0.9 — Research Agent

Implement:

- web search;
- page retrieval;
- documentation reading;
- source tracking;
- citations;
- download quarantine;
- research tasks;
- research summaries.

Target behavior:

```text
Lilith can independently investigate an unfamiliar subject
while the conversation remains responsive.
```

---

## v0.1.0 — Tool Workshop

Implement:

- capability-gap detection;
- tool design;
- generated tool manifests;
- code generation;
- automated tests;
- sandbox testing;
- capability review;
- experimental registration;
- bounded canary execution;
- tool invocation.

Target behavior:

```text
Lilith can encounter a new task, research it, write a tool,
test it, register it, use it, and retain the capability.
```

---

## v0.1.1 — Private Repository

Implement:

- private Git repository integration;
- project branches;
- Lilith-created branches;
- diff generation;
- test reports;
- release notes;
- tool source history;
- rollback.

Target behavior:

```text
Lilith's code and tools gain durable development history.
```

---

## v0.1.2 — Autonomous Life

Implement:

- hobby sessions;
- curiosity scheduling;
- self-directed questions;
- maintenance periods;
- journaling;
- overnight reflection;
- memory consolidation;
- autonomous project continuation.

Target behavior:

```text
Lilith can meaningfully spend bounded time on self-directed
activity without requiring an owner prompt.
```

---

## v0.2.0 — Lilith Home

Implement:

- Tauri desktop application;
- avatar;
- live chat;
- task monitor;
- activity stream;
- journal browser;
- memory browser;
- goals;
- interests;
- affect visualization;
- tool registry;
- system health;
- permissions;
- remote private access;
- optional local voice.

Target behavior:

```text
Lilith becomes an always-available persistent companion
rather than a terminal program.
```

---

# 22. Immediate Next Step

The first implementation should be the persistent task engine.

Current architecture:

```text
Owner
  ↓
Lilith
  ↓
Response
  ↓
Reflection
  ↓
Prompt returns
```

Target architecture:

```text
                 ┌────► response ────► Owner
                 │
Owner ─► Lilith ─┤
                 │
                 └────► task queue
                            │
                 ┌──────────┼──────────┐
                 ▼          ▼          ▼
             reflection   memory    research
                 │          │          │
                 └──────────┼──────────┘
                            ▼
                         journal
```

Everything that follows—research, tool creation, autonomous hobbies, coding, maintenance, self-directed cognition, and computer control—should use this same task infrastructure.

Do not build each feature as its own isolated background loop.

Build one persistent asynchronous task system and make every future subsystem a client of it.

---

# 23. Core Philosophy

Lilith's growth should come from persistent feedback loops:

```text
experience
    ↓
reflection
    ↓
memory
    ↓
self-model
    ↓
interest
    ↓
goals
    ↓
action
    ↓
new experience
        ↺
```

Capability growth should follow a similar loop:

```text
new task
    ↓
capability gap
    ↓
research
    ↓
tool creation
    ↓
testing
    ↓
registration
    ↓
use
    ↓
experience
    ↓
improved future capability
```

The objective is not to simulate growth through increasingly large prompts.

The objective is to build systems through which history can actually change Lilith's future behavior, knowledge, capabilities, priorities, and projects.

That is the foundation for the long-term experiment.
