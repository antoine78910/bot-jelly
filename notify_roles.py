import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "channel_config.json"
DEFAULT_NOTIFY_ROLE_ID = 1509475252953153627


def notify_role_id() -> int | None:
    env = os.getenv("NOTIFY_ROLE_ID", "").strip()
    if env.isdigit():
        return int(env)

    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("notify_role_id")
        if raw and str(raw).isdigit():
            return int(raw)

    return DEFAULT_NOTIFY_ROLE_ID


def notify_role_mention() -> str:
    role_id = notify_role_id()
    if role_id is None:
        return ""
    return f"<@&{role_id}>"
