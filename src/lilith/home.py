"""A detachable Textual home for Lilith's persistent runtime."""
import contextlib
import io
import json
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Header, Input, ProgressBar, Static, TabbedContent, TabPane

from lilith.commands import handle_command
from lilith.config import get_database_path
from lilith.database import Database
from lilith.service import RuntimeState, ensure_service


class LilithHome(App):
    TITLE = "Lilith"
    SUB_TITLE = "a place to talk, think, and keep growing"
    BINDINGS = [("ctrl+q", "quit", "Close window"), ("ctrl+l", "focus_message", "Message"),
                ("ctrl+p", "toggle_curiosity", "Pause / resume curiosity"), ("ctrl+x", "stop_reply", "Stop reply")]
    CSS = """
    Screen { background: #11121b; color: #e4e1ec; }
    Header { background: #242235; color: #ddd0f5; }
    #presence { height: 3; padding: 1 2; color: #bfb3d5; }
    #body { height: 1fr; }
    #conversation { width: 54%; border: round #75628e; margin: 0 1 0 1; }
    #transcript { height: 1fr; padding: 0 1; }
    .message { height: auto; margin: 1 0; padding: 0 1; }
    .owner { border-left: thick #817798; color: #c5c0d2; }
    .lilith { border-left: thick #b992df; color: #f2eafa; }
    .note { color: #a5c3ba; }
    #composer { height: 3; margin: 0 1; }
    #message { width: 1fr; }
    #send { min-width: 8; width: 8; margin-left: 1; }
    #monitor { width: 46%; margin-right: 1; border: round #4e526b; }
    #work-status { height: auto; max-height: 3; padding: 0 1; }
    #work-progress { height: 1; margin: 0 1; }
    #activity-feed { height: auto; }
    TabPane { padding: 1; }
    #tasks { height: 45%; min-height: 6; }
    #task-detail-scroll { height: 1fr; border-top: solid #414459; padding-top: 1; }
    #task-detail { height: auto; }
    #task-buttons { height: 3; }
    #task-buttons Button { min-width: 12; margin-right: 1; }
    #journal, #self-view, #tools-view, #health { height: auto; }
    #bottom { height: 3; padding: 0 2; }
    #bottom Button { margin-right: 1; }
    Footer { background: #242235; }
    Screen.narrow #body { layout: vertical; }
    Screen.narrow #conversation { width: 100%; height: 52%; }
    Screen.narrow #monitor { width: 100%; height: 48%; }
    """

    def __init__(self, database_path=None, *, start_service=True):
        super().__init__()
        self.db = Database(Path(database_path) if database_path else get_database_path())
        self.state = RuntimeState(self.db)
        self.store = self.state.store
        self.start_service = start_service
        self.message_widgets = {}
        self.selected_task = None
        self.task_snapshot = None
        self.local_note = 0
        self.ready = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Connecting to Lilith…", id="presence")
        with Horizontal(id="body"):
            with Vertical(id="conversation"):
                yield VerticalScroll(id="transcript")
                with Horizontal(id="composer"):
                    yield Input(placeholder="Chat, ask me to build a tool, or research something…", id="message")
                    yield Button("Send", id="send", variant="primary")
            with Vertical(id="monitor"):
                yield Static("Background work · ready", id="work-status", markup=False)
                yield ProgressBar(total=100, show_eta=False, id="work-progress")
                with TabbedContent():
                    with TabPane("Live activity", id="activity-tab"):
                        with VerticalScroll():
                            yield Static("Ask naturally: ‘Create a tool that…’\nKeep chatting while I research, build, and test it.", id="activity-feed", markup=False)
                    with TabPane("Tasks", id="tasks-tab"):
                        yield DataTable(id="tasks", cursor_type="row", zebra_stripes=True)
                        with Horizontal(id="task-buttons"):
                            yield Button("Cancel task", id="cancel-task")
                            yield Button("Priority +10", id="boost-task")
                        with VerticalScroll(id="task-detail-scroll"):
                            yield Static("Select a task to see its plan, results, and errors.", id="task-detail", markup=False)
                    with TabPane("Journal", id="journal-tab"):
                        with VerticalScroll():
                            yield Static("", id="journal", markup=False)
                    with TabPane("Self", id="self-tab"):
                        with VerticalScroll():
                            yield Static("", id="self-view", markup=False)
                    with TabPane("Tools", id="tools-tab"):
                        with VerticalScroll():
                            yield Static("", id="tools-view", markup=False)
                    with TabPane("Health", id="health-tab"):
                        with VerticalScroll():
                            yield Static("", id="health", markup=False)
        with Horizontal(id="bottom"):
            yield Button("Pause curiosity", id="curiosity")
            yield Button("Stop reply", id="stop-reply")
            yield Button("Close window", id="close")
        yield Footer()

    async def on_mount(self):
        self.screen.set_class(self.size.width < 100, "narrow")
        table = self.query_one("#tasks", DataTable)
        table.add_columns("ID", "Work", "State", "Priority")
        if self.start_service:
            try:
                import asyncio
                await asyncio.to_thread(ensure_service, self.db)
            except Exception as error:
                await self.note(str(error))
        self.ready = True
        if not self.db.recent_messages(limit=1) and not self.store.rows("SELECT id FROM inbox LIMIT 1"):
            await self.note("Welcome home. Say hello below. Tasks and journal entries will appear here as Lilith works; closing this window leaves her runtime running.")
        await self.refresh_views()
        self.set_interval(0.2, self.refresh_views)
        self.query_one("#message", Input).focus()

    def on_resize(self, event):
        self.screen.set_class(event.size.width < 100, "narrow")

    async def note(self, text):
        self.local_note += 1
        await self.query_one("#transcript", VerticalScroll).mount(Static(text, classes="message note", markup=False))

    async def refresh_views(self):
        if not self.ready:
            return
        status = self.state.status()
        settings = self.state.settings()
        active = self.store.rows("SELECT id,type,state,priority FROM tasks WHERE state NOT IN ('completed','failed','cancelled','needs_review') ORDER BY priority DESC,id")
        recent = self.store.rows("SELECT id,type,state,priority FROM tasks WHERE state IN ('completed','failed','cancelled','needs_review') ORDER BY id DESC LIMIT 60")
        presence = "Here, quietly. Models wake when there's something to do." if status.get("alive") else "Runtime offline. Your conversation and tasks are saved."
        if status.get("alive") and active:
            presence = f"Here with you · {len(active)} background / conversation task(s) · {active[0]['type']} {active[0]['state']}"
        self.query_one("#presence", Static).update(presence)
        from lilith.activity import activity_text, work_progress
        work = self.store.rows("""SELECT id FROM tasks WHERE type IN ('workshop','research','tool_invocation')
            AND (parent_task_id IS NULL OR origin='curiosity')
            ORDER BY CASE WHEN state NOT IN ('completed','failed','cancelled','needs_review') THEN 0 ELSE 1 END,
            id DESC LIMIT 6""")
        if work:
            current = self.store.get(work[0]["id"])
            percent, label = work_progress(self.store, current)
            self.query_one("#work-status", Static).update(f"#{current['id']} · {label} · chat stays available")
            self.query_one("#work-progress", ProgressBar).update(total=100 if percent is not None else None, progress=percent or 0)
            self.query_one("#activity-feed", Static).update("\n\n────────\n\n".join(
                activity_text(self.store, self.store.get(row["id"])) for row in work))
        self.query_one("#curiosity", Button).label = "Pause curiosity" if settings["curiosity_enabled"] else "Resume curiosity"
        transcript = self.query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        requests = self.store.rows("SELECT * FROM inbox ORDER BY id DESC LIMIT 80")[::-1]
        if not requests and not self.message_widgets:
            for index, message in enumerate(self.db.recent_messages(limit=20)):
                widget = Static(("You" if message["role"] == "user" else "Lilith") + "\n" + message["content"],
                                classes="message " + ("owner" if message["role"] == "user" else "lilith"), markup=False)
                await transcript.mount(widget)
                self.message_widgets[("history", index)] = widget
        for request in requests:
            for role in ("owner", "lilith"):
                key = (request["id"], role)
                if role == "owner":
                    text = "You\n" + request["message"]
                else:
                    text = "Lilith\n" + (request["response"] or ("Getting my thoughts together…" if request["state"] == "streaming" else "Queued…"))
                    if request["state"] == "streaming":
                        text += " ▍"
                    if request["state"] in {"cancelled", "failed", "needs_review"}:
                        text = "Lilith\n" + request["response"] + "\n[Reply interrupted] " + (request["error"] or request["state"])
                if key not in self.message_widgets:
                    widget = Static(text, classes="message " + role, markup=False)
                    self.message_widgets[key] = widget
                    await transcript.mount(widget)
                else:
                    self.message_widgets[key].update(text)
        if follow:
            transcript.scroll_end(animate=False)
        snapshot = json.dumps(active + recent)
        if snapshot != self.task_snapshot:
            table = self.query_one("#tasks", DataTable)
            table.clear()
            for row in active + recent:
                table.add_row(str(row["id"]), row["type"], row["state"], str(row["priority"]), key=str(row["id"]))
            self.task_snapshot = snapshot
        if self.selected_task:
            detail = json.dumps(self.store.get(self.selected_task), indent=2, ensure_ascii=False)
            self.query_one("#task-detail", Static).update(detail[:16000])
        journal = self.store.rows("SELECT timestamp,title,body FROM journal_entries ORDER BY id DESC LIMIT 12")
        self.query_one("#journal", Static).update("Reflections, research summaries, and chosen intentions\n\n" +
            "\n\n────────\n\n".join(f"{r['timestamp'][:19]} · {r['title'] or 'Journal'}\n{r['body'][:6000]}" for r in journal))
        from lilith.self_state import SelfState
        self.query_one("#self-view", Static).update(SelfState(self.db).render())
        from lilith.tool_registry import ToolRegistry
        self.query_one("#tools-view", Static).update(json.dumps(ToolRegistry(self.db, self.store).list(), indent=2))
        health = {"runtime": status, "schedule": settings, "model_residency": "on demand (LILITH_KEEP_ALIVE defaults to 0)",
                  "workers": self.store.rows("SELECT * FROM workers ORDER BY heartbeat DESC LIMIT 8")}
        self.query_one("#health", Static).update(json.dumps(health, indent=2))

    async def submit_message(self):
        field = self.query_one("#message", Input)
        text = field.value.strip()
        if not text:
            return
        field.value = ""
        if text in {"/quit", "/exit"}:
            self.exit()
            return
        if text == "/shutdown":
            with self.store.transaction() as c:
                c.execute("UPDATE runtime_status SET stop_requested=1 WHERE id=1")
            await self.note("Runtime shutdown requested. This window can stay open.")
            return
        if text.startswith("/"):
            from types import SimpleNamespace
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                handled = handle_command(text, self.store, SimpleNamespace(error=None))
            if handled:
                await self.note(output.getvalue())
                return
        try:
            if self.start_service and not self.state.status()["alive"]:
                import asyncio
                await asyncio.to_thread(ensure_service, self.db)
            self.state.submit(text)
        except Exception as error:
            await self.note(str(error))
        await self.refresh_views()

    async def on_input_submitted(self, event: Input.Submitted):
        await self.submit_message()

    async def on_button_pressed(self, event: Button.Pressed):
        button = event.button.id
        if button == "send":
            await self.submit_message()
        elif button == "close":
            self.exit()
        elif button == "curiosity":
            await self.action_toggle_curiosity()
        elif button == "stop-reply":
            self.action_stop_reply()
        elif button == "cancel-task" and self.selected_task:
            self.store.cancel(self.selected_task)
        elif button == "boost-task" and self.selected_task:
            task = self.store.get(self.selected_task)
            if task["state"] == "queued":
                with self.store.transaction() as c:
                    c.execute("UPDATE tasks SET priority=MIN(100,priority+10) WHERE id=? AND state='queued'", (task["id"],))

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        self.selected_task = int(event.row_key.value)

    def action_focus_message(self):
        self.query_one("#message", Input).focus()

    async def action_toggle_curiosity(self):
        enabled = not self.state.settings()["curiosity_enabled"]
        self.state.set("curiosity_enabled", enabled)
        await self.note("Curiosity resumed." if enabled else "Curiosity paused; current autonomous work is being cancelled.")

    def action_stop_reply(self):
        for request in self.store.rows("SELECT task_id FROM inbox WHERE state='streaming' AND task_id IS NOT NULL"):
            self.store.cancel(request["task_id"])

    def on_unmount(self):
        self.ready = False
        self.db.close()


def main():
    LilithHome().run()


if __name__ == "__main__":
    main()
