"""Discord posts for new creator videos and the inactive-creator digest."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import discord

from embed_utils import EMBED_DESCRIPTION_LIMIT, MAX_EMBEDS_PER_MESSAGE

CONFIG_PATH = Path(__file__).parent / "channel_config.json"

ACCENT_COLOR = 0x2EC4B6
DEFAULT_CREATOR_VIDEO_CHANNEL_ID = 1568899413471002786
EMBED_TOTAL_CHAR_LIMIT = 6000
VIDEO_ID_CACHE_SIZE = 1024
INACTIVE_TITLE = "Créateurs sans post depuis plus de 24 h"
EMPTY_INACTIVE_TEXT = "Tous les créateurs actifs ont posté."

_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLATFORMS = {"tiktok": "TikTok", "instagram": "Instagram"}
_LONG_DASHES = ("—", "–")

_seen_video_ids: OrderedDict[str, None] = OrderedDict()


class PayloadError(ValueError):
    """The webhook body is not a valid creator video or inactive report."""


class ChannelUnavailable(Exception):
    """The creator video channel cannot accept the message. The sender should retry."""


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _config_int(config_key: str, env_key: str, default: int) -> int:
    env = os.getenv(env_key, "").strip()
    if env.isdigit():
        return int(env)

    raw = _load_config().get(config_key)
    if raw and str(raw).isdigit():
        return int(raw)

    return default


def creator_video_channel_id() -> int:
    return _config_int(
        "creator_video_channel",
        "CREATOR_VIDEO_CHANNEL_ID",
        DEFAULT_CREATOR_VIDEO_CHANNEL_ID,
    )


def _source(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    if any(key in payload for key in keys):
        return payload
    record = payload.get("record")
    if isinstance(record, dict):
        return record
    return payload


def _require_text(source: dict[str, Any], key: str, *, max_len: int) -> str:
    value = source.get(key)
    if value is None:
        raise PayloadError(f"Missing {key}")
    text = " ".join(str(value).split())
    if not text:
        raise PayloadError(f"Missing {key}")
    if len(text) > max_len:
        raise PayloadError(f"Invalid {key}")
    return text


def _clean_name(value: Any) -> str:
    if value is None:
        raise PayloadError("Missing creator_name")
    text = " ".join(str(value).split())
    for dash in _LONG_DASHES:
        text = text.replace(dash, "-")
    text = (
        text.replace("*", "")
        .replace("`", "")
        .replace("[", "(")
        .replace("]", ")")
        .replace("<@", "")
        .replace("<#", "")
    )
    text = re.sub(r"@(everyone|here)\b", "", text, flags=re.IGNORECASE)
    text = " ".join(text.split())
    if not text:
        raise PayloadError("Missing creator_name")
    return text[:200].rstrip()


def _parse_platform(value: Any) -> str:
    if not isinstance(value, str):
        raise PayloadError("Invalid platform")
    platform = value.strip().lower()
    if platform not in _PLATFORMS:
        raise PayloadError("Invalid platform")
    return platform


def _normalize_handle(value: Any) -> str:
    if not isinstance(value, str):
        raise PayloadError("Invalid handle")
    handle = value.strip().lstrip("@")
    if not _HANDLE_RE.fullmatch(handle):
        raise PayloadError("Invalid handle")
    return handle


def _validate_http_url(value: str, *, field: str) -> str:
    text = value.strip()
    if not text or len(text) > 512 or any(char in text for char in " \n\r\t<>)"):
        raise PayloadError(f"Invalid {field}")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PayloadError(f"Invalid {field}")
    return text


def default_profile_url(platform: str, handle: str) -> str:
    if platform == "tiktok":
        return f"https://www.tiktok.com/@{handle}"
    return f"https://www.instagram.com/{handle}/"


def resolve_profile_url(platform: str, handle: str, profile_url: Any) -> str:
    if isinstance(profile_url, str) and profile_url.strip():
        return _validate_http_url(profile_url, field="profile_url")
    return default_profile_url(platform, handle)


def markdown_handle(handle: str, url: str) -> str:
    return f"[@{handle}]({url})"


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None
    if raw.isdigit():
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _require_datetime(value: Any, *, field: str) -> datetime:
    parsed = _parse_datetime(value)
    if parsed is None:
        raise PayloadError(f"Invalid {field}")
    return parsed


def discord_timestamp(moment: datetime) -> str:
    return f"<t:{int(moment.timestamp())}:F>"


def _no_long_dashes(text: str) -> str:
    for dash in _LONG_DASHES:
        text = text.replace(dash, "-")
    return text


@dataclass(frozen=True)
class CreatorVideo:
    video_id: str
    creator_id: str
    creator_name: str
    handle: str
    platform: str
    url: str
    published_at: datetime
    profile_url: str
    creator_admin_url: str | None

    @property
    def platform_label(self) -> str:
        return _PLATFORMS[self.platform]


def parse_creator_video(payload: dict[str, Any]) -> CreatorVideo:
    source = _source(payload, "video_id")
    platform = _parse_platform(source.get("platform"))
    handle = _normalize_handle(source.get("handle"))
    admin_raw = source.get("creator_admin_url")
    admin_url = None
    if isinstance(admin_raw, str) and admin_raw.strip():
        admin_url = _validate_http_url(admin_raw, field="creator_admin_url")

    return CreatorVideo(
        video_id=_require_text(source, "video_id", max_len=256),
        creator_id=_require_text(source, "creator_id", max_len=256),
        creator_name=_clean_name(source.get("creator_name")),
        handle=handle,
        platform=platform,
        url=_validate_http_url(_require_text(source, "url", max_len=512), field="url"),
        published_at=_require_datetime(source.get("published_at"), field="published_at"),
        profile_url=resolve_profile_url(platform, handle, source.get("profile_url")),
        creator_admin_url=admin_url,
    )


def build_video_embed(video: CreatorVideo) -> discord.Embed:
    title = f"Nouvelle vidéo de {video.creator_name}"
    if len(title) > 256:
        title = title[:253].rstrip() + "..."

    embed = discord.Embed(title=_no_long_dashes(title), color=ACCENT_COLOR)
    embed.add_field(name="Plateforme", value=video.platform_label, inline=True)
    embed.add_field(
        name="Compte",
        value=markdown_handle(video.handle, video.profile_url),
        inline=True,
    )
    embed.add_field(name="Publiée", value=discord_timestamp(video.published_at), inline=True)
    embed.set_footer(text="JobShift Creators")
    return embed


def build_video_view(video: CreatorVideo) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Voir la vidéo",
            style=discord.ButtonStyle.link,
            url=video.url,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Voir le compte",
            style=discord.ButtonStyle.link,
            url=video.profile_url,
        )
    )
    if video.creator_admin_url:
        view.add_item(
            discord.ui.Button(
                label="Fiche créateur",
                style=discord.ButtonStyle.link,
                url=video.creator_admin_url,
            )
        )
    return view


def build_sample_creator_video() -> CreatorVideo:
    handle = "marie.jobsearch"
    platform = "tiktok"
    return CreatorVideo(
        video_id=f"test-{uuid.uuid4()}",
        creator_id="test-creator-001",
        creator_name="Marie Dupont",
        handle=handle,
        platform=platform,
        url=f"https://www.tiktok.com/@{handle}/video/1234567890123456789",
        published_at=datetime.now(timezone.utc),
        profile_url=default_profile_url(platform, handle),
        creator_admin_url=None,
    )


@dataclass(frozen=True)
class SocialHandle:
    platform: str
    handle: str
    profile_url: str

    @property
    def platform_label(self) -> str:
        return _PLATFORMS[self.platform]


@dataclass(frozen=True)
class InactiveCreator:
    creator_id: str
    creator_name: str
    handles: tuple[SocialHandle, ...]
    last_published_at: datetime | None
    affiliate_only: bool


@dataclass(frozen=True)
class InactiveReport:
    as_of: datetime
    date_label: str
    creators: tuple[InactiveCreator, ...]


def _parse_affiliate_only(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no", ""):
            return False
    raise PayloadError("Invalid affiliate_only")


def _parse_handles(value: Any) -> tuple[SocialHandle, ...]:
    if not isinstance(value, list):
        raise PayloadError("Invalid handles")
    if len(value) > 10:
        raise PayloadError("Invalid handles")

    handles: list[SocialHandle] = []
    for item in value:
        if not isinstance(item, dict):
            raise PayloadError("Invalid handles")
        platform = _parse_platform(item.get("platform"))
        handle = _normalize_handle(item.get("handle"))
        handles.append(
            SocialHandle(
                platform=platform,
                handle=handle,
                profile_url=resolve_profile_url(platform, handle, item.get("profile_url")),
            )
        )
    return tuple(handles)


def _parse_as_of(value: Any) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value.strip():
        raise PayloadError("Missing date")
    raw = value.strip()
    parsed = _parse_datetime(raw)
    if parsed is None:
        raise PayloadError("Invalid date")
    if _DATE_ONLY_RE.fullmatch(raw):
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=0)
    return parsed, parsed.date().isoformat()


def _parse_last_published(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    parsed = _parse_datetime(value)
    if parsed is None:
        raise PayloadError("Invalid last_published_at")
    return parsed


def parse_inactive_report(payload: dict[str, Any]) -> InactiveReport:
    source = _source(payload, "creators", "date")
    as_of, date_label = _parse_as_of(source.get("date"))
    raw_creators = source.get("creators")
    if not isinstance(raw_creators, list):
        raise PayloadError("Missing creators")

    creators: list[InactiveCreator] = []
    for item in raw_creators:
        if not isinstance(item, dict):
            raise PayloadError("Invalid creators")
        creators.append(
            InactiveCreator(
                creator_id=_require_text(item, "creator_id", max_len=256),
                creator_name=_clean_name(item.get("creator_name")),
                handles=_parse_handles(item.get("handles")),
                last_published_at=_parse_last_published(item.get("last_published_at")),
                affiliate_only=_parse_affiliate_only(item.get("affiliate_only")),
            )
        )
    return InactiveReport(as_of=as_of, date_label=date_label, creators=tuple(creators))


def last_post_phrase(last_published_at: datetime | None, now: datetime) -> str:
    if last_published_at is None:
        return "jamais posté"
    delta = now - last_published_at
    days = delta.days if delta.total_seconds() >= 0 else 0
    if days <= 0:
        return "dernier post il y a moins d'un jour"
    if days == 1:
        return "dernier post il y a 1 jour"
    return f"dernier post il y a {days} jours"


def format_inactive_creator(creator: InactiveCreator, now: datetime) -> str:
    header = f"**{creator.creator_name}**"
    if creator.affiliate_only:
        header += " (affiliation uniquement)"

    links: list[str] = []
    for social in creator.handles:
        link = f"{social.platform_label} {markdown_handle(social.handle, social.profile_url)}"
        candidate = " · ".join(links + [link])
        if len(candidate) > 900:
            break
        links.append(link)

    lines = [header]
    if links:
        lines.append(" · ".join(links))
    hidden_handles = len(creator.handles) - len(links)
    if hidden_handles == 1:
        lines.append("et 1 autre compte")
    elif hidden_handles > 1:
        lines.append(f"et {hidden_handles} autres comptes")
    lines.append(last_post_phrase(creator.last_published_at, now))
    return _no_long_dashes("\n".join(lines))


def _overflow_note(hidden: int) -> str:
    if hidden <= 1:
        return "et 1 autre créateur non affiché"
    return f"et {hidden} autres créateurs non affichés"


def _try_add_note(descriptions: list[str], note: str, *, budget: int) -> list[str] | None:
    if len(note) > budget or len(note) > EMBED_DESCRIPTION_LIMIT:
        return None
    if not descriptions:
        return [note]

    total = sum(len(item) for item in descriptions)
    last = descriptions[-1]
    appended = f"{last}\n\n{note}"
    if len(appended) <= EMBED_DESCRIPTION_LIMIT and total - len(last) + len(appended) <= budget:
        return descriptions[:-1] + [appended]
    if (
        len(descriptions) < MAX_EMBEDS_PER_MESSAGE
        and total + len(note) <= budget
        and len(note) <= EMBED_DESCRIPTION_LIMIT
    ):
        return descriptions + [note]
    return None


def pack_creator_blocks(blocks: list[str], *, budget: int) -> list[str]:
    """Pack creator blocks into embed descriptions that fit in one Discord message."""
    pending = [block.strip() for block in blocks if block.strip()]
    if budget < 1:
        budget = 1

    chosen: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    used = 0

    for block in pending:
        if len(block) > EMBED_DESCRIPTION_LIMIT:
            break
        sep = 2 if current else 0
        need = sep + len(block)
        if current and current_len + need > EMBED_DESCRIPTION_LIMIT:
            if len(chosen) + 1 >= MAX_EMBEDS_PER_MESSAGE:
                break
            chosen.append(current)
            current = []
            current_len = 0
            sep = 0
            need = len(block)
        if used + need > budget or len(chosen) >= MAX_EMBEDS_PER_MESSAGE:
            break
        current.append(block)
        current_len += need
        used += need

    if current and len(chosen) < MAX_EMBEDS_PER_MESSAGE:
        chosen.append(current)

    shown = sum(len(group) for group in chosen)
    hidden = len(pending) - shown
    if hidden <= 0:
        return ["\n\n".join(group) for group in chosen if group]

    while True:
        note = _overflow_note(hidden)
        descriptions = ["\n\n".join(group) for group in chosen if group]
        fitted = _try_add_note(descriptions, note, budget=budget)
        if fitted is not None:
            return fitted
        if not chosen:
            clipped = note[: min(len(note), budget, EMBED_DESCRIPTION_LIMIT)]
            return [clipped]
        if chosen[-1]:
            chosen[-1].pop()
            hidden += 1
            if not chosen[-1]:
                chosen.pop()
        else:
            chosen.pop()


def embed_char_count(embed: discord.Embed) -> int:
    total = len(embed.title or "") + len(embed.description or "")
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    if embed.author and embed.author.name:
        total += len(embed.author.name)
    for field in embed.fields:
        total += len(field.name) + len(field.value)
    return total


def _within_message_limit(embeds: list[discord.Embed]) -> list[discord.Embed]:
    embeds = embeds[:MAX_EMBEDS_PER_MESSAGE]
    while len(embeds) > 1 and sum(embed_char_count(item) for item in embeds) > EMBED_TOTAL_CHAR_LIMIT:
        embeds.pop()
    if embeds and sum(embed_char_count(item) for item in embeds) > EMBED_TOTAL_CHAR_LIMIT:
        embed = embeds[0]
        overflow = sum(embed_char_count(item) for item in embeds) - EMBED_TOTAL_CHAR_LIMIT
        description = embed.description or ""
        keep = max(0, len(description) - overflow - 3)
        embed.description = (description[:keep].rstrip() + "...") if keep else "..."
    return embeds


def build_inactive_embeds(report: InactiveReport) -> list[discord.Embed]:
    footer = f"Rapport {report.date_label}" if report.date_label else ""
    if not report.creators:
        embed = discord.Embed(
            title=INACTIVE_TITLE,
            description=EMPTY_INACTIVE_TEXT,
            color=ACCENT_COLOR,
        )
        if footer:
            embed.set_footer(text=footer)
        return [embed]

    blocks = [format_inactive_creator(creator, report.as_of) for creator in report.creators]
    budget = EMBED_TOTAL_CHAR_LIMIT - len(INACTIVE_TITLE) - len(footer)
    descriptions = pack_creator_blocks(blocks, budget=budget) or [EMPTY_INACTIVE_TEXT]

    embeds: list[discord.Embed] = []
    for index, description in enumerate(descriptions):
        embeds.append(
            discord.Embed(
                title=INACTIVE_TITLE if index == 0 else None,
                description=description,
                color=ACCENT_COLOR,
            )
        )
    if footer and embeds:
        embeds[-1].set_footer(text=footer)
    return _within_message_limit(embeds)


def remember_video(video_id: str) -> bool:
    """Store `video_id`. Return False when it was already in the cache."""
    if video_id in _seen_video_ids:
        _seen_video_ids.move_to_end(video_id)
        return False
    _seen_video_ids[video_id] = None
    _seen_video_ids.move_to_end(video_id)
    while len(_seen_video_ids) > VIDEO_ID_CACHE_SIZE:
        _seen_video_ids.popitem(last=False)
    return True


def forget_video(video_id: str) -> None:
    _seen_video_ids.pop(video_id, None)


def video_already_posted(video_id: str) -> bool:
    if video_id not in _seen_video_ids:
        return False
    _seen_video_ids.move_to_end(video_id)
    return True


def clear_seen_videos() -> None:
    _seen_video_ids.clear()


async def resolve_creator_video_channel(client: discord.Client) -> discord.TextChannel | None:
    channel_id = creator_video_channel_id()
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            print(f"Creator video channel {channel_id} not found")
            return None
    if isinstance(channel, discord.TextChannel):
        return channel
    print(f"Creator video channel {channel_id} is not a text channel")
    return None


async def post_creator_video(client: discord.Client, video: CreatorVideo) -> discord.Message:
    channel = await resolve_creator_video_channel(client)
    if channel is None:
        raise ChannelUnavailable(f"Creator video channel {creator_video_channel_id()} unavailable")

    try:
        message = await channel.send(
            embed=build_video_embed(video),
            view=build_video_view(video),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException as exc:
        raise ChannelUnavailable(str(exc)) from exc

    print(f"Posted creator video {video.video_id} for {video.creator_name}")
    return message


async def post_inactive_creators(client: discord.Client, report: InactiveReport) -> discord.Message:
    channel = await resolve_creator_video_channel(client)
    if channel is None:
        raise ChannelUnavailable(f"Creator video channel {creator_video_channel_id()} unavailable")

    embeds = build_inactive_embeds(report)
    try:
        message = await channel.send(
            embeds=embeds,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException as exc:
        raise ChannelUnavailable(str(exc)) from exc

    label = "creator" if len(report.creators) == 1 else "creators"
    print(f"Posted inactive creator digest ({len(report.creators)} {label})")
    return message
