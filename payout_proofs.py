import hashlib
import re
from datetime import date
from pathlib import Path

import discord
from PIL import Image

PAYOUT_PROOFS_TEMPLATE = "payout_proofs"
PROOFS_DIR = Path(__file__).parent / "assets" / "payout_proofs"

PROOF_FILENAMES = [f"{i:02d}.png" for i in range(1, 13)]

# Earliest transfer date visible on each screenshot (from image headers).
PROOF_EARLIEST_DATES: dict[str, tuple[int, int, int]] = {
    "12.png": (2025, 11, 5),
    "06.png": (2025, 11, 28),
    "01.png": (2025, 12, 4),
    "10.png": (2025, 12, 7),
    "08.png": (2025, 12, 27),
    "02.png": (2026, 1, 2),
    "05.png": (2026, 1, 22),
    "11.png": (2026, 1, 25),
    "03.png": (2026, 1, 31),
    "04.png": (2026, 2, 18),
    "09.png": (2026, 2, 24),
    "07.png": (2026, 3, 1),
}

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_DATE_HEADER_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)


def _parse_date_header(text: str, *, default_year: int = 2026) -> date | None:
    match = _DATE_HEADER_RE.search(text)
    if not match:
        return None
    month = _MONTHS[match.group(1).lower()[:3]]
    day = int(match.group(2))
    year = int(match.group(3)) if match.group(3) else default_year
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _ocr_proof_dates(path: Path) -> list[date]:
    """Best-effort OCR of date headers; returns [] if tesseract is unavailable."""
    try:
        import pytesseract
    except ImportError:
        return []

    image = Image.open(path)
    width, height = image.size
    # Date headers sit above each Revolut card near the top of the screenshot.
    crop = image.crop((0, 0, width, int(height * 0.95)))
    text = pytesseract.image_to_string(crop)
    dates: list[date] = []
    for line in text.splitlines():
        parsed = _parse_date_header(line)
        if parsed is not None:
            dates.append(parsed)
    return dates


def _proof_sort_key(path: Path) -> tuple[date, str]:
    manual = PROOF_EARLIEST_DATES.get(path.name)
    if manual is not None:
        return date(*manual), path.name

    ocr_dates = _ocr_proof_dates(path)
    if ocr_dates:
        return min(ocr_dates), path.name

    return date(9999, 12, 31), path.name


def _collect_proof_paths() -> list[Path]:
    paths: list[Path] = []
    for name in PROOF_FILENAMES:
        path = PROOFS_DIR / name
        if path.is_file():
            paths.append(path)
    if paths:
        return paths
    return list(PROOFS_DIR.glob("*.png")) if PROOFS_DIR.is_dir() else []


def list_proof_image_paths() -> list[Path]:
    """Return payout proof images sorted by earliest transfer date on each screenshot."""
    return sorted(_collect_proof_paths(), key=_proof_sort_key)


def proof_images_fingerprint() -> str:
    parts: list[str] = []
    for path in list_proof_image_paths():
        stat = path.stat()
        sort_key = _proof_sort_key(path)[0].isoformat()
        parts.append(f"{sort_key}:{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


async def publish_payout_proofs(
    channel: discord.TextChannel,
    bot_user: discord.ClientUser,
    *,
    force: bool = False,
) -> list[int]:
    """Post payout proof screenshots (one image per message)."""
    images = list_proof_image_paths()
    if not images:
        raise FileNotFoundError(
            f"No images in {PROOFS_DIR}. Add 01.png–12.png payout screenshots."
        )

    from publish_sync import sync_image_messages

    return await sync_image_messages(channel, bot_user, images)
