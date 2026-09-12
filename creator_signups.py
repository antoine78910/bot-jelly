"""Staff notifications when a creator signs up on the JobShift creator platform."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord

from notify_roles import notify_role_mention

CONFIG_PATH = Path(__file__).parent / "channel_config.json"

SIGNUP_COLOR = 0xFEE75C
DEFAULT_CREATORS_GUILD_ID = 1540823867818385468
DEFAULT_SIGNUP_LOG_CHANNEL_ID = 1547101182680629268


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _config_int(config_key: str, env_key: str, default: int) -> int:
    env = os.getenv(env_key, "").strip()
    if env.isdigit():
        return int(env)

    raw = _load_config().get(config_key)
    if raw and str(raw).isdigit():
        return int(raw)

    return default


def creators_guild_id() -> int:
    return _config_int(
        "creators_guild_id",
        "CREATORS_GUILD_ID",
        DEFAULT_CREATORS_GUILD_ID,
    )


def signup_log_channel_id() -> int:
    return _config_int(
        "creator_signup_log_channel",
        "CREATOR_SIGNUP_LOG_CHANNEL_ID",
        DEFAULT_SIGNUP_LOG_CHANNEL_ID,
    )


def _signup_log_channel(client: discord.Client) -> discord.TextChannel | None:
    channel = client.get_channel(signup_log_channel_id())
    if isinstance(channel, discord.TextChannel):
        return channel
    return None


async def creators_invite_url(client: discord.Client) -> str | None:
    env = os.getenv("CREATOR_DISCORD_INVITE_URL", "").strip()
    if env:
        return env

    guild = client.get_guild(creators_guild_id())
    if guild is None:
        return None

    for channel in guild.text_channels:
        if not channel.permissions_for(guild.me).create_instant_invite:
            continue
        try:
            invite = await channel.create_invite(
                max_age=7 * 24 * 3600,
                max_uses=0,
                unique=True,
                reason="Creator signup invite link",
            )
            return invite.url
        except discord.HTTPException:
            continue

    return None


@dataclass
class CreatorSignup:
    email: str
    country: str
    accounts: str
    user_id: str | None = None
    full_name: str | None = None
    signed_up_at: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CreatorSignup:
        record = payload.get("record") if isinstance(payload.get("record"), dict) else payload

        email = (
            record.get("email")
            or record.get("user_email")
            or record.get("contact_email")
            or "—"
        )
        country = (
            record.get("country")
            or record.get("country_code")
            or record.get("country_name")
            or "—"
        )

        accounts = _format_accounts(record)
        user_id = record.get("id") or record.get("user_id")
        full_name = (
            record.get("full_name")
            or record.get("display_name")
            or record.get("name")
        )
        signed_up_at = record.get("created_at") or record.get("signed_up_at")

        email_value = str(email).strip() if email is not None else ""
        country_value = str(country).strip() if country is not None else ""

        return cls(
            email=email_value or "—",
            country=country_value or "—",
            accounts=accounts,
            user_id=str(user_id) if user_id else None,
            full_name=str(full_name) if full_name else None,
            signed_up_at=str(signed_up_at) if signed_up_at else None,
        )


def _format_accounts(record: dict[str, Any]) -> str:
    raw_accounts = record.get("accounts") or record.get("social_accounts")
    if isinstance(raw_accounts, list) and raw_accounts:
        lines: list[str] = []
        for item in raw_accounts:
            if isinstance(item, dict):
                platform = item.get("platform") or item.get("type") or "Account"
                handle = item.get("handle") or item.get("username") or item.get("url") or "—"
                lines.append(f"• **{platform}:** {handle}")
            else:
                lines.append(f"• {item}")
        return "\n".join(lines) if lines else "—"

    if isinstance(raw_accounts, str) and raw_accounts.strip():
        return raw_accounts.strip()

    parts: list[str] = []
    for key, label in (
        ("instagram", "Instagram"),
        ("tiktok", "TikTok"),
        ("youtube", "YouTube"),
        ("twitter", "Twitter/X"),
        ("linkedin", "LinkedIn"),
    ):
        value = record.get(key)
        if value:
            parts.append(f"• **{label}:** {value}")

    return "\n".join(parts) if parts else "—"


def build_signup_embed(signup: CreatorSignup) -> discord.Embed:
    embed = discord.Embed(
        title="New creator signup",
        description="A user just signed up on the JobShift creator platform.",
        color=SIGNUP_COLOR,
    )

    if signup.full_name:
        embed.add_field(name="Name", value=signup.full_name, inline=True)
    embed.add_field(name="Email", value=signup.email, inline=True)
    embed.add_field(name="Country", value=signup.country, inline=True)
    embed.add_field(name="Accounts", value=signup.accounts[:1024], inline=False)

    if signup.user_id:
        embed.add_field(name="Platform user ID", value=f"`{signup.user_id}`", inline=False)
    if signup.signed_up_at:
        embed.add_field(name="Signed up at", value=signup.signed_up_at, inline=False)

    embed.set_footer(text="JobShift Creators · signup feed")
    embed.timestamp = discord.utils.utcnow()
    return embed


def build_signup_view(invite_url: str | None) -> discord.ui.View | None:
    if not invite_url:
        return None

    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Invite to Discord Creators",
            style=discord.ButtonStyle.link,
            url=invite_url,
        )
    )
    return view


async def log_creator_signup(
    client: discord.Client,
    signup: CreatorSignup,
    *,
    invite_url: str | None = None,
) -> discord.Message | None:
    channel = _signup_log_channel(client)
    if channel is None:
        print(f"Creator signup log channel {signup_log_channel_id()} not found")
        return None

    if invite_url is None:
        invite_url = await creators_invite_url(client)

    embed = build_signup_embed(signup)
    view = build_signup_view(invite_url)
    ping = notify_role_mention()

    return await channel.send(content=ping or None, embed=embed, view=view)


def verify_webhook_secret(request_headers: dict[str, str]) -> bool:
    secret = os.getenv("CREATOR_SIGNUP_WEBHOOK_SECRET", "").strip()
    if not secret:
        return True

    header = (
        request_headers.get("Authorization", "")
        or request_headers.get("authorization", "")
        or request_headers.get("X-Webhook-Secret", "")
        or request_headers.get("x-webhook-secret", "")
    ).strip()

    if header.startswith("Bearer "):
        header = header[7:].strip()

    return header == secret


async def handle_signup_webhook(client: discord.Client, payload: dict[str, Any]) -> bool:
    event_type = payload.get("type")
    if event_type and event_type != "INSERT":
        return False

    signup = CreatorSignup.from_payload(payload)
    if signup.email == "—" and signup.accounts == "—":
        return False

    await log_creator_signup(client, signup)
    return True
