"""Staff activity feeds: Discord joins and content generator outputs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import discord

from notify_roles import notify_role_mention

CONFIG_PATH = Path(__file__).parent / "channel_config.json"

DEFAULT_JOIN_LOG_CHANNEL_ID = 0
DEFAULT_CONTENT_LOG_CHANNEL_ID = 0

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
    summary: str
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
        title="Nouveau membre",
        description=f"{member.mention} a rejoint le serveur.",
        color=JOIN_COLOR,
    )
    embed.add_field(name="Utilisateur", value=f"{member} (`{member.id}`)", inline=True)
    embed.add_field(name="Compte créé", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
    embed.add_field(name="Membres", value=str(member_count), inline=True)
    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="Feed arrivées Discord")
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

    mode_label = "Générer un lot" if mode == "batch" else "Générer du contenu"
    embed = discord.Embed(
        title="Contenu généré",
        description=(
            f"{member.mention} a généré **{created}/{requested}** carrousel"
            f"{'s' if requested != 1 else ''} via **{mode_label}**.\n"
            f"Fil privé : {thread.mention}"
        ),
        color=CONTENT_COLOR,
    )
    embed.add_field(name="Utilisateur", value=f"{member} (`{member.id}`)", inline=True)
    embed.add_field(name="Mode", value=mode_label, inline=True)
    embed.add_field(name="Fil", value=thread.mention, inline=True)

    for output in outputs[:5]:
        delivery = "Upload Discord"
        if output.delivery_mode == "external" and output.url:
            delivery = f"[Lien externe]({output.url}) (72h)"
        value = f"{output.summary}\n**Livraison :** {delivery}"
        embed.add_field(name=output.label, value=value[:1024], inline=False)

    embed.set_footer(text="Feed générateur de contenu")
    embed.timestamp = discord.utils.utcnow()

    ping = notify_role_mention()
    await channel.send(content=ping or None, embed=embed)
