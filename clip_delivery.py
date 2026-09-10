"""Deliver generated clips to Discord with external-host fallback."""

from __future__ import annotations

from pathlib import Path
import zipfile

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
    if suffix == ".zip":
        return "application/zip"
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


SLIDE_LABELS = ("Accroche", "Google", "JobShift", "Récap")
SLIDE_SLUGS = ("accroche", "google", "jobshift", "recap")


def _slide_filename(index: int, label: str) -> str:
    if 1 <= index <= len(SLIDE_SLUGS):
        slug = SLIDE_SLUGS[index - 1]
    else:
        slug = "".join(ch if ch.isalnum() else "_" for ch in label.lower()).strip("_")
    return f"slide_{index:02d}_{slug or 'slide'}.png"


def _slide_names(slides: list[Path]) -> list[tuple[Path, str]]:
    named: list[tuple[Path, str]] = []
    for index, path in enumerate(slides, start=1):
        label = SLIDE_LABELS[index - 1] if index <= len(SLIDE_LABELS) else f"Slide {index}"
        named.append((path, _slide_filename(index, label)))
    return named


def _build_carousel_zip(slides: list[Path], dest: Path) -> Path:
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, filename in _slide_names(slides):
            archive.write(path, arcname=filename)
    return dest


def _zip_download_view(url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Télécharger le ZIP",
            style=discord.ButtonStyle.link,
            url=url,
            emoji="⬇️",
        )
    )
    return view


async def _zip_url(slides: list[Path]) -> str:
    zip_path = slides[0].parent / "carousel.zip"
    _build_carousel_zip(slides, zip_path)
    return await upload_to_external_host(zip_path)


async def deliver_carousel_to_thread(
    thread: discord.Thread,
    member: discord.Member,
    slides: list[Path],
    *,
    clip_label: str,
) -> tuple[str, str | None]:
    """Send the 4 slides as one Discord album, plus a ZIP download button."""
    if len(slides) < 1:
        raise CarouselAssemblyError("Aucune slide de carrousel à envoyer.")

    missing = [str(path) for path in slides if not path.is_file()]
    if missing:
        raise CarouselAssemblyError(f"Fichiers carrousel manquants : {', '.join(missing)}")

    caption = f"{member.mention} 🎠 **{clip_label}**"
    files = [
        discord.File(path, filename=filename)
        for path, filename in _slide_names(slides)
    ]

    zip_link: str | None = None
    try:
        zip_link = await _zip_url(slides)
    except ClipAssemblyError:
        zip_link = None

    view = _zip_download_view(zip_link) if zip_link else None

    try:
        await thread.send(caption, files=files, view=view)
        if zip_link is None:
            zip_path = slides[0].parent / "carousel.zip"
            if not zip_path.is_file():
                _build_carousel_zip(slides, zip_path)
            await thread.send(
                "⬇️ **ZIP des 4 slides**",
                file=discord.File(zip_path, filename="carousel.zip"),
            )
        return ("discord", zip_link)
    except discord.HTTPException as exc:
        if not is_payload_too_large(exc):
            raise

    if zip_link is None:
        zip_link = await _zip_url(slides)

    await thread.send(
        f"{caption} — trop lourd pour Discord en album, "
        f"télécharge le ZIP (lien valable **{EXTERNAL_LINK_TTL}**) :\n{zip_link}",
        view=_zip_download_view(zip_link),
        suppress_embeds=True,
    )
    return "external", zip_link


async def _prepare(path: Path, *, emergency: bool) -> Path:
    import asyncio

    return await asyncio.to_thread(prepare_for_discord_upload, path, emergency=emergency)
