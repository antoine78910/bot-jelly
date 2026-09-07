"""Deliver generated clips to Discord with external-host fallback."""

from __future__ import annotations

from pathlib import Path

import aiohttp
import discord

from clip_assembler import ClipAssemblyError, prepare_for_discord_upload

LITTERBOX_API = "https://litterbox.catbox.moe/resources/internals/api.php"
EXTERNAL_LINK_TTL = "72h"


def is_payload_too_large(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "413" in text or "40005" in text or "entity too large" in text:
        return True
    if isinstance(exc, discord.HTTPException):
        return exc.status == 413 or exc.code == 40005
    return False


async def upload_to_external_host(path: Path) -> str:
    """Upload a clip to litterbox.catbox.moe (public URL, valid 72 hours)."""
    if not path.is_file():
        raise ClipAssemblyError(f"File not found: {path}")

    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    form.add_field("time", EXTERNAL_LINK_TTL)
    form.add_field(
        "fileToUpload",
        path.read_bytes(),
        filename=path.name,
        content_type="video/mp4",
    )

    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(LITTERBOX_API, data=form) as response:
            body = (await response.text()).strip()

    if not body.startswith("https://"):
        raise ClipAssemblyError(f"External upload failed: {body[:300]}")

    return body


async def deliver_clip_to_thread(
    thread: discord.Thread,
    member: discord.Member,
    path: Path,
    *,
    clip_label: str,
) -> tuple[str, str | None]:
    """
    Try Discord upload (with compression), then emergency compression, then litterbox.

    Returns (mode, url) where mode is \"discord\" or \"external\".
    """
    compressed = await _prepare(path, emergency=False)

    try:
        await thread.send(
            f"{member.mention} 🎬",
            file=discord.File(compressed, filename=compressed.name),
        )
        return "discord", None
    except discord.HTTPException as exc:
        if not is_payload_too_large(exc):
            raise

    emergency = await _prepare(compressed, emergency=True)
    try:
        await thread.send(
            f"{member.mention} 🎬",
            file=discord.File(emergency, filename=emergency.name),
        )
        return "discord", None
    except discord.HTTPException as exc:
        if not is_payload_too_large(exc):
            raise

    url = await upload_to_external_host(emergency)
    await thread.send(
        f"{member.mention} 🎬 **{clip_label}** — too large for Discord, "
        f"download here (link valid **{EXTERNAL_LINK_TTL}**):\n{url}",
        suppress_embeds=True,
    )
    return "external", url


async def _prepare(path: Path, *, emergency: bool) -> Path:
    import asyncio

    return await asyncio.to_thread(prepare_for_discord_upload, path, emergency=emergency)
