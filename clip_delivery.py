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
        f"{member.mention} 🎬 **{clip_label}** — trop lourd pour Discord, "
        f"télécharge ici (lien valable **{EXTERNAL_LINK_TTL}**) :\n{url}",
        suppress_embeds=True,
    )
    return "external", url


SLIDE_LABELS = ("Accroche", "Google", "Jellyjob", "Récap")


def _slide_filename(index: int, label: str) -> str:
    slug = "".join(ch if ch.isalnum() else "_" for ch in label.lower()).strip("_")
    return f"slide_{index:02d}_{slug or 'slide'}.png"


def _download_view(items: list[tuple[str, str]]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for label, url in items[:5]:
        view.add_item(
            discord.ui.Button(
                label=f"Télécharger {label}",
                style=discord.ButtonStyle.link,
                url=url,
                emoji="⬇️",
            )
        )
    return view


async def _send_slide_image(
    thread: discord.Thread,
    path: Path,
    *,
    filename: str,
    caption: str,
) -> tuple[str, str]:
    """Post one PNG so Discord shows the native download control. Fallback: external URL."""
    try:
        message = await thread.send(
            caption,
            file=discord.File(path, filename=filename),
        )
        if message.attachments:
            return "discord", message.attachments[0].url
        return "discord", ""
    except discord.HTTPException as exc:
        if not is_payload_too_large(exc):
            raise

    url = await upload_to_external_host(path)
    await thread.send(
        f"{caption} — trop lourd pour Discord, télécharge ici "
        f"(lien valable **{EXTERNAL_LINK_TTL}**) :\n{url}",
        suppress_embeds=True,
    )
    return "external", url


async def deliver_carousel_to_thread(
    thread: discord.Thread,
    member: discord.Member,
    slides: list[Path],
    *,
    clip_label: str,
) -> tuple[str, str | None]:
    """
    Send each slide as its own image (native Discord download), plus Download buttons.
    """
    if len(slides) < 1:
        raise CarouselAssemblyError("Aucune slide de carrousel à envoyer.")

    missing = [str(path) for path in slides if not path.is_file()]
    if missing:
        raise CarouselAssemblyError(f"Fichiers carrousel manquants : {', '.join(missing)}")

    await thread.send(f"{member.mention} 🎠 **{clip_label}**")

    download_items: list[tuple[str, str]] = []
    used_external = False
    for index, path in enumerate(slides, start=1):
        label = SLIDE_LABELS[index - 1] if index <= len(SLIDE_LABELS) else f"Slide {index}"
        filename = _slide_filename(index, label)
        caption = f"**{label}** ({index}/{len(slides)})"
        mode, url = await _send_slide_image(
            thread,
            path,
            filename=filename,
            caption=caption,
        )
        if mode == "external":
            used_external = True
        if url:
            download_items.append((label, url))

    if download_items:
        await thread.send(
            "⬇️ **Appuie pour télécharger chaque slide**",
            view=_download_view(download_items),
        )

    if not download_items:
        raise CarouselAssemblyError("Impossible d’envoyer les slides du carrousel.")

    return ("external" if used_external else "discord"), download_items[0][1]


async def _prepare(path: Path, *, emergency: bool) -> Path:
    import asyncio

    return await asyncio.to_thread(prepare_for_discord_upload, path, emergency=emergency)
