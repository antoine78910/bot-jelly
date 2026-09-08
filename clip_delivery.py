"""Deliver generated clips to Discord with external-host fallback."""

from __future__ import annotations

from pathlib import Path

import aiohttp
import discord

from clip_assembler import ClipAssemblyError, prepare_for_discord_upload
from carousel_assembler import CarouselAssemblyError

LITTERBOX_API = "https://litterbox.catbox.moe/resources/internals/api.php"
EXTERNAL_LINK_TTL = "72h"


def is_payload_too_large(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "413" in text or "40005" in text or "entity too large" in text:
        return True
    if isinstance(exc, discord.HTTPException):
        return exc.status == 413 or exc.code == 40005
    return False


def _content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".mp4":
        return "video/mp4"
    return "application/octet-stream"


async def upload_to_external_host(path: Path) -> str:
    """Upload a file to litterbox.catbox.moe (public URL, valid 72 hours)."""
    if not path.is_file():
        raise ClipAssemblyError(f"File not found: {path}")

    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    form.add_field("time", EXTERNAL_LINK_TTL)
    form.add_field(
        "fileToUpload",
        path.read_bytes(),
        filename=path.name,
        content_type=_content_type_for(path),
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


def _discord_files(slides: list[Path], clip_label: str) -> list[discord.File]:
    slug = clip_label.replace(" ", "_").lower()
    return [
        discord.File(path, filename=f"{slug}_{path.name}")
        for path in slides
    ]


async def deliver_carousel_to_thread(
    thread: discord.Thread,
    member: discord.Member,
    slides: list[Path],
    *,
    clip_label: str,
) -> tuple[str, str | None]:
    """
    Send 4 carousel slides as a Discord album, then one-by-one, then external links.
    """
    if len(slides) < 1:
        raise CarouselAssemblyError("No carousel slides to upload.")

    missing = [str(path) for path in slides if not path.is_file()]
    if missing:
        raise CarouselAssemblyError(f"Carousel files missing: {', '.join(missing)}")

    try:
        await thread.send(
            f"{member.mention} 🎠 **{clip_label}**",
            files=_discord_files(slides, clip_label),
        )
        return "discord", None
    except discord.HTTPException as exc:
        if not is_payload_too_large(exc):
            raise

    try:
        await thread.send(f"{member.mention} 🎠 **{clip_label}**")
        for path in slides:
            await thread.send(file=discord.File(path, filename=path.name))
        return "discord", None
    except discord.HTTPException as exc:
        if not is_payload_too_large(exc):
            raise

    urls: list[str] = []
    for path in slides:
        urls.append(await upload_to_external_host(path))
    joined = "\n".join(f"• {url}" for url in urls)
    await thread.send(
        f"{member.mention} 🎠 **{clip_label}** — too large for Discord, "
        f"download here (links valid **{EXTERNAL_LINK_TTL}**):\n{joined}",
        suppress_embeds=True,
    )
    return "external", urls[0] if urls else None


async def _prepare(path: Path, *, emergency: bool) -> Path:
    import asyncio

    return await asyncio.to_thread(prepare_for_discord_upload, path, emergency=emergency)
