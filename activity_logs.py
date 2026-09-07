"""Staff activity feeds: Discord joins and content generator outputs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import discord

from clip_assembler import ClipRecipe, recipe_summary
from notify_roles import notify_role_mention

CONFIG_PATH = Path(__file__).parent / "channel_config.json"

DEFAULT_JOIN_LOG_CHANNEL_ID = 1510936933575163984
DEFAULT_CONTENT_LOG_CHANNEL_ID = 1510937017582620672

JOIN_COLOR = 0x57F287
CONTENT_COLOR = 0x5865F2


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _channel_id(config_key: str, env_key: str, default: int) -> int:
    env = os.getenv(env_key, "").strip()
    if env.isdigit():
        return int(env)

    raw = _load_config().get(config_key)
    if raw and str(raw).isdigit():
        return int(raw)

    return default


def join_log_channel_id() -> int:
    return _channel_id(
        "discord_join_log_channel",
        "DISCORD_JOIN_LOG_CHANNEL_ID",
        DEFAULT_JOIN_LOG_CHANNEL_ID,
    )


def content_log_channel_id() -> int:
    return _channel_id(
        "content_generation_log_channel",
        "CONTENT_GENERATION_LOG_CHANNEL_ID",
        DEFAULT_CONTENT_LOG_CHANNEL_ID,
    )


def _log_channel(client: discord.Client, channel_id: int) -> discord.TextChannel | None:
    channel = client.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel
    return None


@dataclass
class ClipOutput:
    label: str
    recipe: ClipRecipe
    delivery_mode: str
    url: str | None = None


async def log_member_join(client: discord.Client, member: discord.Member) -> None:
    if member.bot:
        return
    channel = _log_channel(client, join_log_channel_id())
    if channel is None:
        return

    guild = member.guild
    member_count = guild.member_count or len(guild.members)

    embed = discord.Embed(
        title="New member joined",
        description=f"{member.mention} joined the server.",
        color=JOIN_COLOR,
    )
    embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=True)
    embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
    embed.add_field(name="Member count", value=str(member_count), inline=True)
    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="Discord join feed")
    embed.timestamp = discord.utils.utcnow()

    ping = notify_role_mention()
    await channel.send(content=ping or None, embed=embed)


async def log_content_generation(
    client: discord.Client,
    member: discord.Member,
    *,
    mode: str,
    thread: discord.Thread,
    outputs: list[ClipOutput],
    created: int,
    requested: int,
) -> None:
    if created < 1 or not outputs:
        return

    channel = _log_channel(client, content_log_channel_id())
    if channel is None:
        return

    mode_label = "Batch generate" if mode == "batch" else "Generate content"
    embed = discord.Embed(
        title="Content generated",
        description=(
            f"{member.mention} generated **{created}/{requested}** clip"
            f"{'s' if requested != 1 else ''} via **{mode_label}**.\n"
            f"Private thread: {thread.mention}"
        ),
        color=CONTENT_COLOR,
    )
    embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=True)
    embed.add_field(name="Mode", value=mode_label, inline=True)
    embed.add_field(name="Thread", value=thread.mention, inline=True)

    for output in outputs[:5]:
        delivery = "Discord upload"
        if output.delivery_mode == "external" and output.url:
            delivery = f"[External link]({output.url}) (72h)"
        value = f"{recipe_summary(output.recipe)}\n**Delivery:** {delivery}"
        embed.add_field(name=output.label, value=value[:1024], inline=False)

    embed.set_footer(text="Content generator feed")
    embed.timestamp = discord.utils.utcnow()

    ping = notify_role_mention()
    await channel.send(content=ping or None, embed=embed)
