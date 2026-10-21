"""Deliver generated clips to Discord with external-host fallback."""

from __future__ import annotations

from pathlib import Path
import zipfile

import aiohttp
import discord

from clip_assembler import ClipAssemblyError, prepare_for_discord_upload
from carousel_assembler import CarouselAssemblyError

LITTERBOX_API = "https://litterbox.catbox.moe/resources/internals/api.php"
CATBOX_API = "https://catbox.moe/user/api.php"
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
    """Upload a file outside Discord. Tries catbox, then litterbox."""
    if not path.is_file():
        raise ClipAssemblyError(f"File not found: {path}")

    payload = path.read_bytes()
    timeout = aiohttp.ClientTimeout(total=300)
    errors: list[str] = []
    targets = ((CATBOX_API, None), (LITTERBOX_API, EXTERNAL_LINK_TTL))

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url, ttl in targets:
            form = aiohttp.FormData()
            form.add_field("reqtype", "fileupload")
            if ttl:
                form.add_field("time", ttl)
            form.add_field(
                "fileToUpload",
                payload,
                filename=path.name,
                content_type=_content_type_for(path),
            )
            try:
                async with session.post(url, data=form) as response:
                    body = (await response.text()).strip()
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                continue
            if body.startswith("https://"):
                return body
            errors.append(f"{url}: {body[:180]}")

    raise ClipAssemblyError(f"External upload failed: {' | '.join(errors)[:300]}")


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
    classic = len(slides) == len(SLIDE_LABELS)
    for index, path in enumerate(slides, start=1):
        if classic:
            named.append((path, _slide_filename(index, SLIDE_LABELS[index - 1])))
        else:
            named.append((path, f"slide_{index:02d}.png"))
    return named


def _build_carousel_zip(slides: list[Path], dest: Path) -> Path:
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, filename in _slide_names(slides):
            archive.write(path, arcname=filename)
    return dest


def _download_view(url: str, *, label: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.link,
            url=url,
            emoji="⬇️",
        )
    )
    return view


async def _self_hosted_url(slides: list[Path], clip_label: str) -> str | None:
    import asyncio

    from carousel_hosting import hosting_available, publish_carousel

    if not hosting_available():
        return None
    try:
        return await asyncio.to_thread(
            publish_carousel, _slide_names(slides), title=clip_label
        )
    except Exception as exc:
        print(f"Carousel self-hosting failed for {clip_label}: {exc}")
        return None


async def deliver_carousel_to_thread(
    thread: discord.Thread,
    member: discord.Member,
    slides: list[Path],
    *,
    clip_label: str,
) -> tuple[str, str | None]:
    """Post a link to the original PNGs hosted outside Discord. No image attachments."""
    from carousel_hosting import LINK_TTL_LABEL

    if len(slides) < 1:
        raise CarouselAssemblyError("Aucune slide de carrousel à envoyer.")

    missing = [str(path) for path in slides if not path.is_file()]
    if missing:
        raise CarouselAssemblyError(f"Fichiers carrousel manquants : {', '.join(missing)}")

    caption = f"{member.mention} 🎠 **{clip_label}** — {len(slides)} slides"

    gallery = await _self_hosted_url(slides, clip_label)
    if gallery:
        await thread.send(
            f"{caption}\n"
            "Images en qualité d’origine (PNG), hors Discord : ouvre le lien pour les voir "
            f"et les enregistrer. Lien valable **{LINK_TTL_LABEL}**.",
            view=_download_view(gallery, label="Télécharger les images"),
            suppress_embeds=True,
        )
        return "external", gallery

    zip_path = slides[0].parent / "carousel.zip"
    _build_carousel_zip(slides, zip_path)
    try:
        zip_link = await upload_to_external_host(zip_path)
    except Exception as exc:
        print(f"Original ZIP upload failed for {clip_label}: {exc}")
        zip_link = None

    if zip_link:
        await thread.send(
            f"{caption}\n"
            "PNG d’origine dans le ZIP, hors Discord. "
            f"Lien valable **{EXTERNAL_LINK_TTL}**.",
            view=_download_view(zip_link, label="Télécharger le ZIP"),
            suppress_embeds=True,
        )
        return "external", zip_link

    # Last resort: a ZIP attachment is not recompressed by Discord, unlike images.
    await thread.send(
        f"{caption}\n"
        "Le lien externe n’a pas pu être créé. Voici le ZIP des PNG d’origine.",
        file=discord.File(zip_path, filename="carrousel.zip"),
    )
    return "discord", None


async def _prepare(path: Path, *, emergency: bool) -> Path:
    import asyncio

    return await asyncio.to_thread(prepare_for_discord_upload, path, emergency=emergency)
