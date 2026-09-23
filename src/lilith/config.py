from pathlib import Path
import os


APP_NAME = "Lilith"
DEFAULT_MODEL = "huihui_ai/qwen3-abliterated:4b"

def get_data_dir() -> Path:
    override = os.environ.get("LILITH_DATA_DIR")

    if override:
        path = Path(override).expanduser()
    else:
        local_app_data = os.environ.get("LOCALAPPDATA")

        if local_app_data:
            path = Path(local_app_data) / APP_NAME
        else:
            path = Path.home() / APP_NAME

    path.mkdir(parents=True, exist_ok=True)
    return path


def get_database_path() -> Path:
    return get_data_dir() / "lilith.db"


def get_model_name() -> str:
    return os.environ.get("LILITH_MODEL", DEFAULT_MODEL)


def get_role_model(role: str) -> str:
    key = {
        "conversation": "LILITH_CONVERSATION_MODEL",
        "router": "LILITH_ROUTER_MODEL",
        "reflection": "LILITH_REFLECTION_MODEL",
        "reasoning": "LILITH_REASONING_MODEL",
        "research": "LILITH_RESEARCH_MODEL",
        "workshop": "LILITH_WORKSHOP_MODEL",
    }.get(role)
    return os.environ.get(key, get_model_name()) if key else get_model_name()


def get_workspace() -> Path:
    path = Path(os.environ.get("LILITH_WORKSPACE", str(get_data_dir() / "workspace"))).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path
