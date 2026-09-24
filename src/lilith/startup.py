"""Per-user Windows login startup, with no administrator or system service required."""
import os
from pathlib import Path
import sys

from lilith.config import get_data_dir, get_model_name, get_workspace


def startup_path():
    if os.name != "nt" or not os.environ.get("APPDATA"):
        raise RuntimeError("Login startup integration currently supports Windows")
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/Lilith.vbs"


def install():
    target = startup_path()
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.exists():
        python = Path(sys.executable)
    def quote(value):
        return '"' + str(value).replace('"', '""') + '"'
    import subprocess
    command = subprocess.list2cmdline([str(python), "-B", "-m", "lilith.service", "start"])
    script = ["' Lilith per-user persistent runtime", 'Set shell = CreateObject("WScript.Shell")',
              'Set env = shell.Environment("PROCESS")',
              f'env("LILITH_DATA_DIR") = {quote(get_data_dir())}',
              f'env("LILITH_WORKSPACE") = {quote(get_workspace())}',
              f'env("LILITH_MODEL") = {quote(get_model_name())}']
    for key in ("LILITH_OLLAMA_URL", "LILITH_CONVERSATION_MODEL", "LILITH_ROUTER_MODEL",
                "LILITH_REFLECTION_MODEL", "LILITH_REASONING_MODEL", "LILITH_RESEARCH_MODEL",
                "LILITH_WORKSHOP_MODEL", "LILITH_ALLOW_SHELL"):
        if os.environ.get(key):
            script.append(f'env("{key}") = {quote(os.environ[key])}')
    script.append(f"shell.Run {quote(command)}, 0, False")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\r\n".join(script) + "\r\n", encoding="utf-16")
    return target


def remove():
    target = startup_path()
    if target.exists():
        target.unlink()
    return target
