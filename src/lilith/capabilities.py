"""Typed, scoped local capabilities; every request and outcome is audited.

Terminal and application grants execute trusted owner code, not a security sandbox.
Model-generated plans receive only the read-only subset.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


class CapabilityError(RuntimeError):
    pass


# Required and optional argument types form the public capability contract.
SPECS = {
    "browser.search": ({"query": str}, {}),
    "browser.navigate": ({"url": str}, {}),
    "browser.read": ({"url": str}, {}),
    "browser.download": ({"url": str}, {}),
    "browser.follow_link": ({"source_id": int, "link_index": int}, {}),
    "tool.invoke": ({"name": str, "data": dict}, {}),
    "filesystem.read": ({"path": str}, {}),
    "filesystem.list": ({"path": str}, {}),
    "filesystem.search": ({"path": str, "pattern": str}, {}),
    "filesystem.write": ({"path": str, "content": str}, {}),
    "filesystem.create": ({"path": str}, {"directory": bool, "content": str}),
    "filesystem.copy": ({"source": str, "destination": str}, {}),
    "filesystem.move": ({"source": str, "destination": str}, {}),
    "filesystem.delete": ({"path": str}, {}),
    "process.list": ({}, {}),
    "process.inspect": ({"pid": int}, {}),
    "process.start": ({"command": str, "arguments": list, "working_directory": str, "expected_effect": str}, {"environment": dict}),
    "process.stop": ({"pid": int}, {}),
    "terminal.run": ({"command": str, "arguments": list, "working_directory": str, "timeout": int,
                      "environment": dict, "expected_effect": str}, {}),
    "app.open": ({"command": str, "arguments": list, "working_directory": str, "expected_effect": str}, {}),
    "app.close": ({"pid": int}, {}),
    "app.focus": ({"window_title": str}, {}),
    "app.restart": ({"pid": int, "command": str, "arguments": list, "working_directory": str, "expected_effect": str}, {}),
    "desktop.screenshot": ({"path": str}, {}),
    "desktop.click": ({"x": int, "y": int}, {"button": str}),
    "desktop.type": ({"text": str}, {}),
    "desktop.keypress": ({"keys": list}, {}),
    "desktop.scroll": ({"amount": int}, {}),
    "desktop.window_list": ({}, {}),
    "development.venv": ({"path": str}, {}),
    "development.test": ({"working_directory": str}, {"directory": str, "timeout": int}),
}
for _name in ("status", "diff", "log"):
    SPECS[f"git.{_name}"] = ({"working_directory": str}, {})
for _name, _field in (("branch", "name"), ("checkout", "name"), ("commit", "message"), ("restore", "path")):
    SPECS[f"git.{_name}"] = ({"working_directory": str, _field: str}, {})

READ_ONLY = {"filesystem.read", "filesystem.list", "filesystem.search", "git.status", "git.diff", "git.log",
             "process.list", "process.inspect", "desktop.window_list", "tool.invoke"}

# Retain local handles until stopped or reaped; the process itself may intentionally
# outlive this worker for process.start/app.open.
_LAUNCHED = {}


class CapabilityBroker:
    def __init__(self, database, workspace, *, allowed=(), task_id=None, storage_limit=1048576):
        self.db = database
        self.root = Path(workspace).resolve()
        self.allowed = set(allowed)
        self.task_id = task_id
        self.remaining_storage = storage_limit

    def path(self, value):
        path = Path(value).expanduser()
        path = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_relative_to(self.root):
            raise CapabilityError("Path is outside the configured workspace")
        return path

    def _reserve(self, size):
        if size > self.remaining_storage:
            raise CapabilityError("Storage budget exceeded")
        self.remaining_storage -= size

    def invoke(self, name, args):
        # Persist intent before dispatch, including rejected requests.
        record = {"task_id": self.task_id, "capability": name, "arguments": args}
        self.db.audit("capability_requested", json.dumps(record), "capability_broker")
        try:
            if name not in SPECS or name not in self.allowed:
                raise CapabilityError(f"Capability not granted: {name}")
            required, optional = SPECS[name]
            if not isinstance(args, dict) or set(args) - (required.keys() | optional.keys()) or required.keys() - args.keys():
                raise CapabilityError("Arguments do not match capability schema")
            for key, value in args.items():
                if type(value) is not (required | optional)[key]:
                    raise CapabilityError(f"Invalid type for {key}")
            result = self._dispatch(name, args)
            self.db.audit("capability_completed", json.dumps({**record, "result": result}), "capability_broker")
            return result
        except Exception as error:
            self.db.audit("capability_failed", json.dumps({**record, "error": str(error)}), "capability_broker")
            raise

    def _run(self, command, arguments, cwd, timeout=30, environment=None):
        if not command or not all(isinstance(a, str) for a in arguments):
            raise CapabilityError("Command and arguments must be strings")
        if not 1 <= timeout <= 120:
            raise CapabilityError("Timeout must be 1–120 seconds")
        env = dict(os.environ)
        if environment:
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()):
                raise CapabilityError("Environment must contain string values")
            env.update(environment)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        # Drain output to disk while monitoring its size, avoiding unbounded PIPE buffers.
        with tempfile.TemporaryFile() as out:
            proc = subprocess.Popen([command, *arguments], cwd=self.path(cwd), env=env, stdout=out,
                                    stderr=subprocess.STDOUT, creationflags=flags)
            started = time.monotonic()
            try:
                while proc.poll() is None:
                    if time.monotonic() - started > timeout or os.fstat(out.fileno()).st_size > 1048576:
                        raise CapabilityError("Command time or output budget exceeded")
                    time.sleep(0.05)
            finally:
                if proc.poll() is None:
                    import psutil
                    try:
                        for child in psutil.Process(proc.pid).children(recursive=True):
                            try:
                                child.kill()
                            except psutil.NoSuchProcess:
                                pass
                    except psutil.NoSuchProcess:
                        pass
                    proc.kill()
                proc.wait()
            out.seek(0)
            return {"returncode": proc.returncode, "output": out.read(65536).decode("utf-8", errors="replace")}

    def _dispatch(self, name, a):
        op = name.split(".")[1]
        if name == "tool.invoke":
            from lilith.tasks import TaskStore
            from lilith.tool_registry import ToolRegistry
            return {"output": ToolRegistry(self.db, TaskStore(self.db)).invoke(a["name"], a["data"], task_id=self.task_id)}
        if name.startswith("browser."):
            from lilith.tasks import TaskStore
            from lilith.research import Research
            store = TaskStore(self.db)
            research = Research(self.db, store)
            if self.task_id is None:
                raise CapabilityError("Research requires a persistent task ID")
            if op == "search":
                return research.search(self.task_id, a["query"])
            if op == "follow_link":
                rows = store.rows("SELECT links FROM research_sources WHERE id=?", (a["source_id"],))
                if not rows or not 0 <= a["link_index"] < len(json.loads(rows[0]["links"])):
                    raise CapabilityError("Unknown source or link index")
                return research.read(self.task_id, json.loads(rows[0]["links"])[a["link_index"]]["url"])
            return research.read(self.task_id, a["url"], download=op == "download")
        if name.startswith("filesystem."):
            p = self.path(a.get("path", a.get("source", ".")))
            if op == "read":
                with p.open("rb") as f:
                    data = f.read(65537)
                return {"content": data[:65536].decode("utf-8", errors="replace"), "truncated": len(data) > 65536}
            if op == "list":
                from itertools import islice
                return {"entries": [str(x.relative_to(self.root)) for x in islice(p.iterdir(), 500)]}
            if op == "search":
                if len(a["pattern"]) > 200 or ".." in Path(a["pattern"]).parts or Path(a["pattern"]).is_absolute():
                    raise CapabilityError("Invalid search pattern")
                from itertools import islice
                return {"entries": [str(self.path(str(x)).relative_to(self.root)) for x in islice(p.glob(a["pattern"]), 500)]}
            if p == self.root:
                raise CapabilityError("Cannot mutate workspace root")
            if op in {"write", "create"}:
                if op == "create" and a.get("directory"):
                    p.mkdir()
                else:
                    data = a.get("content", "").encode("utf-8")
                    self._reserve(len(data))
                    with p.open("xb" if op == "create" else "wb") as f:
                        f.write(data)
                return {"path": str(p), "exists": p.exists()}
            if op in {"copy", "move"}:
                dest = self.path(a["destination"])
                if dest.exists() or not p.is_file():
                    raise CapabilityError("Source must be a file and destination must not exist")
                self._reserve(p.stat().st_size)
                shutil.copy2(p, dest) if op == "copy" else p.rename(dest)
                with dest.open("rb") as f:
                    digest = hashlib.file_digest(f, "sha256").hexdigest()
                return {"path": str(dest), "sha256": digest}
            if op == "delete":
                p.rmdir() if p.is_dir() else p.unlink()
                return {"deleted": not p.exists()}
        if name == "terminal.run":
            if not a["expected_effect"].strip():
                raise CapabilityError("Expected effect is required")
            return self._run(a["command"], a["arguments"], a["working_directory"], a["timeout"], a["environment"])
        if name.startswith("git."):
            cwd = self.path(a["working_directory"])
            git_flags = ["-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "--no-pager"]
            repo = self._run("git", [*git_flags, "rev-parse", "--show-toplevel"], str(cwd))
            if repo["returncode"] != 0:
                raise CapabilityError("Working directory is not in a Git repository")
            self.path(repo["output"].strip())
            commands = {"status": ["status", "--short"], "diff": ["diff", "--no-ext-diff", "--no-textconv"],
                        "log": ["log", "-10", "--oneline"], "commit": ["commit", "-m", a.get("message", "")]}
            if op in {"branch", "checkout"}:
                value = a["name"]
                if value.startswith("-") or not value.strip():
                    raise CapabilityError("Invalid branch name")
                commands[op] = [op, value]
            if op == "restore":
                target = self.path(str(cwd / a["path"]))
                commands[op] = ["restore", "--", str(target.relative_to(cwd))]
            return self._run("git", [*git_flags, *commands[op]], str(cwd))
        if name == "development.venv":
            p = self.path(a["path"])
            if p.exists():
                raise CapabilityError("Environment path must not already exist")
            return self._run(sys.executable, ["-m", "venv", str(p)], str(self.root), 120)
        if name == "development.test":
            cwd = self.path(a["working_directory"])
            directory = self.path(str(cwd / a.get("directory", "tests")))
            return self._run(sys.executable, ["-m", "unittest", "discover", "-s", str(directory)], str(cwd), a.get("timeout", 60))
        if name in {"process.start", "app.open", "app.restart"}:
            for pid, launched in list(_LAUNCHED.items()):
                if launched.poll() is not None:
                    launched.wait()
                    del _LAUNCHED[pid]
            if not all(isinstance(x, str) for x in a["arguments"]) or not a["expected_effect"].strip():
                raise CapabilityError("Invalid process request")
            if name == "app.restart":
                self._stop(a["pid"])
            env = dict(os.environ)
            env.update(a.get("environment", {}))
            proc = subprocess.Popen([a["command"], *a["arguments"]], cwd=self.path(a["working_directory"]),
                                    env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            _LAUNCHED[proc.pid] = proc
            return {"pid": proc.pid, "started": True}
        if name in {"process.stop", "app.close"}:
            return self._stop(a["pid"])
        if name in {"process.list", "process.inspect"}:
            import psutil
            if op == "inspect":
                return psutil.Process(a["pid"]).as_dict(attrs=["pid", "name", "status", "create_time"])
            return {"processes": [p.info for p in psutil.process_iter(["pid", "name", "status"])][:500]}
        if name in {"desktop.window_list", "app.focus"}:
            import pygetwindow
            if name == "desktop.window_list":
                return {"titles": pygetwindow.getAllTitles()[:500]}
            windows = pygetwindow.getWindowsWithTitle(a["window_title"])
            if len(windows) != 1:
                raise CapabilityError("Window title must identify exactly one window")
            windows[0].activate()
            return {"focused": windows[0].title}
        if name.startswith("desktop."):
            import pyautogui
            pyautogui.FAILSAFE = True
            if op == "screenshot":
                p = self.path(a["path"])
                if p.exists():
                    raise CapabilityError("Screenshot destination already exists")
                import io
                data = io.BytesIO()
                pyautogui.screenshot().save(data, format="PNG")
                self._reserve(data.tell())
                p.write_bytes(data.getvalue())
                return {"path": str(p), "bytes": data.tell()}
            if op == "click":
                if not pyautogui.onScreen(a["x"], a["y"]) or a.get("button", "left") not in {"left", "right", "middle"}:
                    raise CapabilityError("Invalid click")
                pyautogui.click(a["x"], a["y"], button=a.get("button", "left"))
            elif op == "type":
                if len(a["text"]) > 10000 or not a["text"].isascii():
                    raise CapabilityError("Desktop typing supports at most 10000 ASCII characters")
                pyautogui.write(a["text"])
            elif op == "keypress":
                if not 1 <= len(a["keys"]) <= 5 or any(k not in pyautogui.KEYBOARD_KEYS for k in a["keys"]):
                    raise CapabilityError("Invalid keys")
                pyautogui.hotkey(*a["keys"])
            elif op == "scroll":
                if abs(a["amount"]) > 100:
                    raise CapabilityError("Scroll exceeds limit")
                pyautogui.scroll(a["amount"])
            return {"dispatched": True}
        raise CapabilityError("Capability not implemented")

    @staticmethod
    def _stop(pid):
        import psutil
        if pid <= 0 or pid in {os.getpid(), os.getppid()}:
            raise CapabilityError("Cannot stop the runtime")
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=5)
        launched = _LAUNCHED.pop(pid, None)
        if launched is not None:
            launched.wait(timeout=5)
        return {"pid": pid, "stopped": True}
