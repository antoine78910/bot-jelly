"""Skip or in-place edit when channel content has not changed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import discord

from embed_utils import build_embeds_from_template
from embeds import get_template

STATE_PATH = Path(__file__).parent / "publish_state.json"
# Bump when publish/skip logic changes so all channels resync once.
SYNC_VERSION = 4


def _hash_payload(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _embed_payload(embed: discord.Embed) -> dict:
    return embed.to_dict()


def _load_state() -> dict[str, dict]:
    if not STATE_PATH.exists():
        return {}
    with STATE_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    return {str(k): v for k, v in data.items()}


def _save_state(state: dict[str, dict]) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_channel_state(channel_id: int | str) -> dict | None:
    return _load_state().get(str(channel_id))


def save_channel_state(
    channel_id: int | str,
    template_name: str,
    fingerprint: str,
    message_ids: list[int],
) -> None:
    state = _load_state()
    state[str(channel_id)] = {
        "template": template_name,
        "fingerprint": fingerprint,
        "message_ids": message_ids,
    }
    _save_state(state)


def _raw_fingerprint(template_name: str) -> str:
    template = get_template(template_name)
    if template is not None:
        return _hash_payload(template)

    if template_name == "payout_proofs":
        from payout_proofs import proof_images_fingerprint

        return proof_images_fingerprint()

    if template_name == "registration_welcome":
        from registration import registration_panel_fingerprint

        return registration_panel_fingerprint()

    if template_name == "payout_submission_welcome":
        from payout_submission import payout_submission_panel_fingerprint

        return payout_submission_panel_fingerprint()

    if template_name == "content_generator_welcome":
        from content_generator import content_generator_panel_fingerprint

        return content_generator_panel_fingerprint()

    raise ValueError(f"Unknown template for fingerprint: {template_name}")


def compute_fingerprint(template_name: str) -> str:
    return _hash_payload({"sync_version": SYNC_VERSION, "content": _raw_fingerprint(template_name)})


def expected_message_count(template_name: str) -> int | None:
    if template_name == "payout_proofs":
        from payout_proofs import list_proof_image_paths

        return len(list_proof_image_paths())

    template = get_template(template_name)
    if template is not None:
        return len(build_embeds_from_template(template))

    return 1


def clear_publish_state(channel_id: int | str | None = None) -> None:
    if channel_id is None:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
        return

    state = _load_state()
    state.pop(str(channel_id), None)
    _save_state(state)


async def count_bot_panel_messages(
    channel: discord.TextChannel,
    bot_user: discord.ClientUser,
    template_name: str,
) -> int:
    count = 0
    is_proofs = template_name == "payout_proofs"
    async for message in channel.history(limit=100):
        if message.author.id != bot_user.id:
            continue
        if is_proofs:
            # Image uploads keep Discord's attachment preview embed — do not require embeds absent.
            if message.attachments:
                count += 1
        elif message.embeds or message.components:
            count += 1
    return count


async def bot_messages_exist(
    channel: discord.TextChannel,
    message_ids: list[int],
) -> bool:
    if not message_ids:
        return False
    for message_id in message_ids:
        try:
            await channel.fetch_message(message_id)
        except discord.NotFound:
            return False
        except discord.HTTPException:
            return False
    return True


async def should_skip_publish(
    channel: discord.TextChannel,
    template_name: str,
    fingerprint: str,
    bot_user: discord.ClientUser,
    *,
    force: bool = False,
) -> bool:
    if force:
        return False

    stored = get_channel_state(channel.id)
    if not stored:
        return False
    if stored.get("template") != template_name:
        return False
    if stored.get("fingerprint") != fingerprint:
        return False

    message_ids = stored.get("message_ids") or []
    if not await bot_messages_exist(channel, message_ids):
        return False

    expected = expected_message_count(template_name)
    if expected is not None:
        visible = await count_bot_panel_messages(channel, bot_user, template_name)
        if visible < expected or len(message_ids) < expected:
            return False

    return True


async def find_tracked_bot_messages(
    channel: discord.TextChannel,
    bot_user: discord.ClientUser,
) -> list[discord.Message]:
    stored = get_channel_state(channel.id)
    if stored and stored.get("message_ids"):
        messages: list[discord.Message] = []
        for message_id in stored["message_ids"]:
            try:
                messages.append(await channel.fetch_message(message_id))
            except (discord.NotFound, discord.HTTPException):
                continue
        if messages:
            return sorted(messages, key=lambda m: m.id)

    found: list[discord.Message] = []
    async for message in channel.history(limit=50):
        if message.author.id == bot_user.id:
            found.append(message)
    return sorted(found, key=lambda m: m.id)


async def sync_embed_messages(
    channel: discord.TextChannel,
    bot_user: discord.ClientUser,
    embeds: list[discord.Embed],
    *,
    view: discord.ui.View | None = None,
) -> list[int]:
    """Edit existing bot panel when possible; otherwise replace bot messages."""
    existing = await find_tracked_bot_messages(channel, bot_user)
    panel_messages = [m for m in existing if m.embeds or m.components]

    if len(panel_messages) == 1 and len(embeds) == 1:
        message = panel_messages[0]
        try:
            await message.edit(embed=embeds[0], view=view)
            return [message.id]
        except discord.HTTPException:
            pass

    if (
        len(panel_messages) == len(embeds)
        and len(embeds) > 0
        and all(m.embeds for m in panel_messages)
        and view is None
    ):
        try:
            for message, embed in zip(panel_messages, embeds, strict=True):
                await message.edit(embed=embed)
            return [m.id for m in panel_messages]
        except discord.HTTPException:
            pass

    from channel_utils import delete_bot_messages

    await delete_bot_messages(channel, bot_user)

    message_ids: list[int] = []
    if view is not None and len(embeds) == 1:
        sent = await channel.send(embed=embeds[0], view=view)
        return [sent.id]

    for i in range(0, len(embeds), 10):
        sent = await channel.send(embeds=embeds[i : i + 10])
        message_ids.append(sent.id)
    return message_ids


async def sync_image_messages(
    channel: discord.TextChannel,
    bot_user: discord.ClientUser,
    image_paths: list[Path],
) -> list[int]:
    """Always replace proof images (Discord attachment edits are unreliable)."""
    from channel_utils import delete_bot_messages

    await delete_bot_messages(channel, bot_user)

    message_ids: list[int] = []
    for path in image_paths:
        sent = await channel.send(
            file=discord.File(path),
            suppress_embeds=False,
        )
        message_ids.append(sent.id)
    return message_ids
