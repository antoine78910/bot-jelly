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


DISCORD_ALBUM_BUDGET = 8_000_000


def _shrink_slide(path: Path, max_bytes: int) -> Path:
    """JPEG copy small enough for a Discord album. Leaves the original in place."""
    if path.stat().st_size <= max_bytes and path.suffix.lower() in {".jpg", ".jpeg"}:
        return path
    from PIL import Image

    dest = path.with_name(f"{path.stem}_discord.jpg")
    image = Image.open(path).convert("RGB")
    quality = 95
    while quality >= 80:
        image.save(dest, "JPEG", quality=quality, optimize=True, subsampling=0)
        if dest.stat().st_size <= max_bytes:
            return dest
        quality -= 5
    return dest


def _slides_for_discord(slides: list[Path]) -> list[tuple[Path, str]]:
    named = _slide_names(slides)
    if not named:
        return []
    per_file = max(350_000, DISCORD_ALBUM_BUDGET // len(named))
    prepared: list[tuple[Path, str]] = []
    for path, filename in named:
        shrunk = _shrink_slide(path, per_file)
        prepared.append((shrunk, Path(filename).with_suffix(shrunk.suffix).name))
    return prepared


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
    zip_path = slides[0].parent / "carousel.zip"
    _build_carousel_zip(slides, zip_path)
    zip_link: str | None = None
    try:
        zip_link = await upload_to_external_host(zip_path)
    except Exception as exc:
        print(f"Original ZIP upload failed for {clip_label}: {exc}")

    prepared = _slides_for_discord(slides)
    files = [discord.File(path, filename=filename) for path, filename in prepared]
    preview_error: discord.HTTPException | None = None

    try:
        await thread.send(caption, files=files)
    except discord.HTTPException as exc:
        if not is_payload_too_large(exc):
            preview_error = exc
        else:
            for index, (path, filename) in enumerate(prepared, start=1):
                label = caption if index == 1 else f"{caption} — slide {index}"
                try:
                    await thread.send(label, file=discord.File(path, filename=filename))
                except discord.HTTPException as nested:
                    if not is_payload_too_large(nested):
                        preview_error = nested
                        break

    note = (
        f"{caption}\n"
        "PNG d’origine dans le ZIP : photo nette, sans grain, sans compression Discord."
    )
    if zip_link:
        note += f"\nLien valable **{EXTERNAL_LINK_TTL}** :\n{zip_link}"

    try:
        await thread.send(
            note,
            file=discord.File(zip_path, filename="carrousel.zip"),
            view=_zip_download_view(zip_link) if zip_link else None,
            suppress_embeds=True,
        )
        return ("external" if zip_link else "discord"), zip_link
    except discord.HTTPException as exc:
        if not is_payload_too_large(exc) or not zip_link:
            if preview_error is not None:
                raise preview_error
            raise
        await thread.send(
            note,
            view=_zip_download_view(zip_link),
            suppress_embeds=True,
        )
        return "external", zip_link


async def _prepare(path: Path, *, emergency: bool) -> Path:
    import asyncio

    return await asyncio.to_thread(prepare_for_discord_upload, path, emergency=emergency)
