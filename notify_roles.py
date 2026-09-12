import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "channel_config.json"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def notify_role_id() -> int | None:
    env = os.getenv("NOTIFY_ROLE_ID", "").strip()
    if env.isdigit():
        return int(env)

    raw = _load_config().get("notify_role_id")
    if raw and str(raw).isdigit():
        return int(raw)

    return None


def notify_role_mention() -> str:
    role_id = notify_role_id()
    if role_id is None:
        return ""
    return f"<@&{role_id}>"


def notify_user_ids() -> list[int]:
    env = os.getenv("NOTIFY_USER_IDS", "").strip()
    if env:
        return [int(part) for part in env.split(",") if part.strip().isdigit()]

    raw = _load_config().get("notify_user_ids") or []
    if isinstance(raw, list):
        return [int(item) for item in raw if str(item).isdigit()]
    if str(raw).isdigit():
        return [int(raw)]
    return []


def notify_mentions(*, include_role: bool = True) -> str:
    parts: list[str] = []
    if include_role:
        role = notify_role_mention()
        if role:
            parts.append(role)
    for user_id in notify_user_ids():
        mention = f"<@{user_id}>"
        if mention not in parts:
            parts.append(mention)
    return " ".join(parts)
