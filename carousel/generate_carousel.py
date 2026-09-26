"""
Carousel generator for TikTok/Instagram — 9:16 images with avatar + lifestyle photos + stacked text.

Type 1 carousel: slide 1 = avatar + hook, slides 2–4 = style/study photos (+ method text on slide 2).

Usage:
  python generate_carousel.py --test              # preview text style on black background
  python generate_carousel.py --ab-test           # optional A/B compare (all 4 side by side)
  python generate_carousel.py --augment random    # default: 1 random effect per carousel
  python generate_carousel.py --augment crop      # force crop|grade|grain|prop|none|random
  python generate_carousel.py --color pink --sets 3
"""

import argparse
import json
import os
import random
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
ASSETS_DIR = PROJECT_ROOT / "assets"
AVATARS_DIR = ASSETS_DIR / "avatars"
PHOTOS_DIR = ASSETS_DIR / "photos"
OVERLAYS_DIR = ASSETS_DIR / "overlays"
FONTS_DIR = PROJECT_ROOT / "fonts"
OUTPUT_DIR = PROJECT_ROOT / "output" / "carousel"
AB_TEST_DIR = OUTPUT_DIR / "ab_test"
CAPTIONS_FILE = PROJECT_ROOT / "carousel_captions.txt"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
TEMPLATE3_FILE = TEMPLATES_DIR / "template3.json"
TEMPLATE4_FILE = TEMPLATES_DIR / "template4.json"
DARK_CITIES_PACK = "dark_cities"
# Night skylines are only for template 3 — keep them out of the lifestyle mix.
PHOTO_PACKS_EXCLUDED_FROM_MIX = frozenset({DARK_CITIES_PACK})

# Avatar pack 1 = femme noir (drop photos in assets/avatars/femme_noir/)
DEFAULT_AVATAR_PACK = "femme_noir"
# Photo packs under assets/photos/ — slides 2–4 pick at random from all packs
DEFAULT_PHOTO_PACK = None  # None = mix every remaining pack under assets/photos/

# Photo augment: random = pick 1 of 4 per carousel (default). A/B compare via --ab-test only.
AUGMENT_METHODS = ("crop", "grade", "grain", "prop")
DEFAULT_AUGMENT = "random"  # one random effect on all slides of a carousel
AB_TEST_ACTIVE = False  # set True to re-enable --ab-test without the "inactive" notice

# ---------------------------------------------------------------------------
# Canvas (9:16 TikTok)
# ---------------------------------------------------------------------------
CANVAS_W = 1080
CANVAS_H = 1920

TYPE1_LIFESTYLE_COUNT = 3


@dataclass
class CarouselBuild:
    slides: list[Path]
    pair_style: str
    augment: str
    color: str
    avatar_name: str
    photo_names: list[str]
    hook_preview: str

# ---------------------------------------------------------------------------
# TikTok text-box style (from reference screenshot)
# ---------------------------------------------------------------------------
TIKTOK_STYLE = {
    "pink": {
        # Slightly more fuchsia
        "box_bg": (0xF9, 0xC8, 0xE0),       # #f9c8e0 — fond un peu plus fuchsia
        "box_text": (0xD4, 0x2A, 0x7A),     # #d42a7a — titre plus fuchsia
        "highlight_text": (255, 255, 255),  # white on pink (e.g. DANGER DE MORT)
        "emphasis_bg": (255, 255, 255),
        "emphasis_text": (0, 0, 0),
    },
    "blue": {
        "box_bg": (191, 219, 254),
        "box_text": (30, 64, 175),
        "highlight_text": (255, 255, 255),
        "emphasis_bg": (255, 255, 255),
        "emphasis_text": (0, 0, 0),
    },
    "green": {
        "box_bg": (187, 247, 208),
        "box_text": (21, 128, 61),
        "highlight_text": (255, 255, 255),
        "emphasis_bg": (255, 255, 255),
        "emphasis_text": (0, 0, 0),
    },
    "cream": {
        # Template 2 — pastilles beurre + texte brun, produit en blanc
        "box_bg": (246, 224, 150),
        "box_text": (139, 90, 36),
        "highlight_text": (255, 255, 255),
        "emphasis_bg": (255, 255, 255),
        "emphasis_text": (17, 17, 17),
    },
}
DEFAULT_COLOR = "pink"
# Template 2 (édition rentrée) uses the cream pills from the reference.
PAIR_STYLE_COLOR = {"template2": "cream"}
BADGE_FONT_SIZE = 42

# Layout — continuous stepped pâté: each line hugs text, one surface per block
SIDE_MARGIN = 48
MAX_TEXT_WIDTH = CANVAS_W - SIDE_MARGIN * 2
TOP_MARGIN = 0.06
BOTTOM_MARGIN = 0.12

PILL_PAD_X = 22          # uniform distance letter↔edge (left/right) — all titles
PILL_PAD_Y = 8           # vertical pad around each line
PILL_RADIUS = 18
SEAM_OVERLAP = 14        # pull consecutive lines closer inside the same pâté

# Gaps BETWEEN pâtés (hooks keep these small — see assets/references/hooks/)
BLOCK_GAP_MIN = 12
BLOCK_GAP_MAX = 24

# Proportional font: short text → bigger, long text → smaller
FONT_SIZE_MAX = 72
FONT_SIZE_MIN = 36
FONT_SIZE_BASE = 52

# Classic TikTok = SemiBold (a bit less bold than Bold)
PREFERRED_FONTS = (
    "TikTokSans-SemiBold.ttf",
    "TikTokSans-Bold.ttf",
    "ProximaNova-Semibold.otf",
    "ProximaNova-Semibold.ttf",
    "Proxima Nova Semibold.otf",
    "Proxima Nova Semibold.ttf",
    "proximanova-semibold.otf",
    "arialbd.ttf",
    "segoeuib.ttf",
)

EMOJI_DIR = ASSETS_DIR / "emoji"
# Apple-style emoji PNGs (hex codepoints, downloaded from emoji-datasource-apple)
APPLE_EMOJI_CDN = "https://cdn.jsdelivr.net/npm/emoji-datasource-apple@15.1.2/img/apple/64/{code}.png"

_FONT_EXTS = frozenset({".ttf", ".otf", ".ttc"})
_IMG_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_EMOJI_CACHE: dict[str, Image.Image] = {}

# Google Emploi hook (slide 1) — !! = white text on pink inside the pâté
REFERENCE_CAPTION = [
    ("Je vais être en|!!DANGER DE 💀 M O R T 💀|après ce post 😂", False),
    ("Voici la méthode qui me|permet de trouver un|travail quand je veux avec|20 minutes d'efforts 📝", False),
]


def _win_fonts() -> Path:
    return Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def get_tiktok_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Prefer TikTok Sans SemiBold (Classic) / Proxima Nova Semibold if present."""
    candidates: list[str] = []
    win = _win_fonts()
    user_fonts = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"
    for name in PREFERRED_FONTS:
        candidates.append(str(FONTS_DIR / name))
        candidates.append(str(win / name))
        candidates.append(str(user_fonts / name))
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.truetype(str(win / "arialbd.ttf"), size)
    except (OSError, IOError):
        return ImageFont.load_default()


def get_emoji_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Unused fallback — Apple emoji PNGs are preferred."""
    return get_tiktok_font(size)


def _emoji_to_code(emoji: str) -> str:
    """Convert emoji chars to apple datasource hex code (e.g. 🚨 → 1f6a8)."""
    parts: list[str] = []
    for ch in emoji:
        o = ord(ch)
        if o in (0xFE0F, 0xFE0E):  # variation selectors
            continue
        parts.append(f"{o:x}")
    return "-".join(parts)


def _load_apple_emoji(emoji: str, size: int) -> Image.Image | None:
    """Load Apple-style emoji PNG (cached), scaled to ~font size."""
    code = _emoji_to_code(emoji.strip())
    if not code:
        return None
    cache_key = f"{code}@{size}"
    if cache_key in _EMOJI_CACHE:
        return _EMOJI_CACHE[cache_key].copy()

    EMOJI_DIR.mkdir(parents=True, exist_ok=True)
    path = EMOJI_DIR / f"{code}.png"
    if not path.exists():
        url = APPLE_EMOJI_CDN.format(code=code)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            path.write_bytes(urllib.request.urlopen(req, timeout=20).read())
        except Exception:
            return None

    try:
        im = Image.open(path).convert("RGBA")
    except Exception:
        return None

    # Scale so emoji matches text cap height (~0.95 of font size)
    target = max(16, int(size * 0.95))
    im = im.resize((target, target), Image.Resampling.LANCZOS)
    _EMOJI_CACHE[cache_key] = im
    return im.copy()


def _is_emoji_char(ch: str) -> bool:
    """True for emoji / pictograph codepoints (incl. 🚨)."""
    o = ord(ch)
    return (
        0x1F300 <= o <= 0x1FAFF
        or 0x2600 <= o <= 0x27BF
        or 0xFE00 <= o <= 0xFE0F
        or 0x1F1E0 <= o <= 0x1F1FF
        or o == 0x200D
    )


def _split_text_runs(text: str) -> list[tuple[str, bool]]:
    """Split into (substring, is_emoji) runs for mixed font drawing."""
    if not text:
        return []
    runs: list[tuple[str, bool]] = []
    buf = text[0]
    emoji = _is_emoji_char(text[0])
    for ch in text[1:]:
        e = _is_emoji_char(ch)
        if e == emoji:
            buf += ch
        else:
            runs.append((buf, emoji))
            buf = ch
            emoji = e
    runs.append((buf, emoji))
    return runs


def _measure(text: str, font) -> tuple[int, int]:
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _text_bbox(text: str, font) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) relative to draw origin."""
    return font.getbbox(text)


def _measure_mixed(text: str, font, emoji_size: int) -> tuple[int, int]:
    w = 0
    h = 0
    for part, is_emoji in _split_text_runs(text):
        if is_emoji:
            em = _load_apple_emoji(part, emoji_size)
            if em is not None:
                pw, ph = em.size
            else:
                pw, ph = emoji_size, emoji_size
        else:
            pw, ph = _measure(part, font)
        w += pw
        h = max(h, ph)
    return w, h if h else 1


def _draw_mixed_text(
    overlay: Image.Image,
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font,
    emoji_size: int,
    fill: tuple,
) -> None:
    """Draw text + Apple emoji PNGs, optically centered on line."""
    x, y = xy
    # Baseline for regular text: use top of first non-emoji run
    line_h = _measure_mixed(text, font, emoji_size)[1]

    for part, is_emoji in _split_text_runs(text):
        if is_emoji:
            em = _load_apple_emoji(part, emoji_size)
            if em is not None:
                ey = y + (line_h - em.size[1]) // 2
                overlay.paste(em, (x, ey), em)
                x += em.size[0]
            continue

        # Correct vertical position using font bbox top offset
        left, top, right, bottom = _text_bbox(part, font)
        tw = right - left
        th = bottom - top
        tx = x - left
        ty = y + (line_h - th) // 2 - top
        draw.text((tx, ty), part, font=font, fill=fill)
        x += tw


def _collect_images(directory: Path, recursive: bool = True) -> list[Path]:
    """Collect readable PNG/JPG/WEBP from a folder (skips missing/corrupt files)."""
    if not directory.is_dir():
        return []
    paths = directory.rglob("*") if recursive else directory.iterdir()
    found: list[Path] = []
    for path in paths:
        if not path.is_file():
            continue
        if path.suffix.lower() not in _IMG_EXTS:
            continue
        if path.name.lower().startswith("readme"):
            continue
        try:
            with Image.open(path) as img:
                img.verify()
        except (OSError, ValueError):
            continue
        found.append(path)
    return sorted(found)


def _avatar_pack_dir(pack: str) -> Path:
    """Resolve avatar pack folder, e.g. femme_noir → assets/avatars/femme_noir."""
    pack = (pack or DEFAULT_AVATAR_PACK).strip()
    if not pack:
        pack = DEFAULT_AVATAR_PACK
    specific = AVATARS_DIR / pack
    if specific.is_dir():
        return specific
    normalized = pack.lower().replace(" ", "_")
    if AVATARS_DIR.is_dir():
        for path in AVATARS_DIR.iterdir():
            if path.is_dir() and path.name.lower().replace(" ", "_") == normalized:
                return path
    return AVATARS_DIR


def _list_avatar_packs() -> list[str]:
    if not AVATARS_DIR.is_dir():
        return []
    packs = [p.name for p in sorted(AVATARS_DIR.iterdir()) if p.is_dir()]
    return packs


def _pick_avatar_pack(rng: random.Random | None = None) -> str:
    """Pick a random avatar pack folder under assets/avatars/."""
    packs = _list_avatar_packs()
    if not packs:
        return DEFAULT_AVATAR_PACK
    picker = rng if rng is not None else random
    return picker.choice(packs)


def _list_photo_packs() -> list[str]:
    if not PHOTOS_DIR.is_dir():
        return []
    return [p.name for p in sorted(PHOTOS_DIR.iterdir()) if p.is_dir()]


_FEMININE_PHOTO_RE = re.compile(r".*\d+f$", re.IGNORECASE)
_HEX_NAME_RE = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)


def _is_feminine_only_photo(path: Path) -> bool:
    """Named files ending in digits + f (study_work_01f.png) are female avatars only.

    Hash filenames are ignored even if they happen to end in hex ``f``.
    """
    stem = path.stem
    if _HEX_NAME_RE.fullmatch(stem):
        return False
    return bool(_FEMININE_PHOTO_RE.match(stem))


_MALE_AVATAR_PREFIXES = ("homme", "tisme", "tismey")


def _is_feminine_avatar_pack(pack: str | None) -> bool:
    name = (pack or "").strip().lower().replace(" ", "_").replace("é", "e")
    if not name:
        return True
    return not name.startswith(_MALE_AVATAR_PREFIXES)


def _collect_photos(pack: str | None = None, *, avatar_pack: str | None = None) -> list[Path]:
    """Slides 2+ : one pack, or every pack under assets/photos/ (random mix).

    Photos whose stem ends with digits + ``f`` are skipped for male avatar
    packs (``homme*`` and ``tisme*`` / tismey).
    """
    if pack:
        photos = _collect_images(PHOTOS_DIR / pack)
    else:
        photos = _collect_mix_photos()
    if _is_feminine_avatar_pack(avatar_pack):
        return photos
    return [path for path in photos if not _is_feminine_only_photo(path)]


def _load_and_cover(path: Path, w: int, h: int) -> Image.Image:
    img = Image.open(path).convert("RGB")
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


# ---------------------------------------------------------------------------
# Photo augmentations (subtle — same scene, looks “new” for IG/TikTok)
# ---------------------------------------------------------------------------

def _augment_crop(img: Image.Image, rng: random.Random) -> Image.Image:
    """Zoom 8–14% + random pan — strongest free anti-duplicate."""
    w, h = img.size
    zoom = rng.uniform(1.08, 1.14)
    nw, nh = int(w * zoom), int(h * zoom)
    scaled = img.resize((nw, nh), Image.Resampling.LANCZOS)
    max_x, max_y = nw - w, nh - h
    left = rng.randint(0, max(0, max_x))
    top = rng.randint(0, max(0, max_y))
    out = scaled.crop((left, top, left + w, top + h))
    if rng.random() < 0.45:
        out = ImageOps.mirror(out)
    angle = rng.uniform(-2.2, 2.2)
    if abs(angle) > 0.3:
        out = out.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(0, 0, 0))
    return out


def _augment_grade(img: Image.Image, rng: random.Random) -> Image.Image:
    """Warm/cool grade + contrast + soft vignette."""
    out = img.copy()
    out = ImageEnhance.Color(out).enhance(rng.uniform(0.88, 1.18))
    out = ImageEnhance.Contrast(out).enhance(rng.uniform(0.92, 1.14))
    out = ImageEnhance.Brightness(out).enhance(rng.uniform(0.94, 1.08))
    # Temperature shift via channel mix
    r, g, b = out.split()
    warm = rng.choice([-1, 1]) * rng.uniform(0.04, 0.12)
    r = r.point(lambda x: max(0, min(255, int(x * (1 + warm)))))
    b = b.point(lambda x: max(0, min(255, int(x * (1 - warm * 0.7)))))
    out = Image.merge("RGB", (r, g, b))
    # Vignette
    vignette = Image.new("L", out.size, 0)
    vd = ImageDraw.Draw(vignette)
    margin = int(min(out.size) * rng.uniform(0.08, 0.18))
    vd.ellipse(
        [-margin, -margin, out.size[0] + margin, out.size[1] + margin],
        fill=255,
    )
    vignette = vignette.filter(ImageFilter.GaussianBlur(radius=min(out.size) // 5))
    dark = Image.new("RGB", out.size, (12, 8, 10))
    strength = rng.uniform(0.18, 0.35)
    inv = vignette.point(lambda p: int(255 - (255 - p) * strength))
    out = Image.composite(out, dark, inv)
    return out


def _augment_grain(img: Image.Image, rng: random.Random) -> Image.Image:
    """Film grain + slight softness (subtle “pixel / analog” feel)."""
    out = img.copy()
    if rng.random() < 0.5:
        out = out.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.4, 0.9)))
    w, h = out.size
    # Coarse noise scaled up = more film-like than per-pixel static
    nw, nh = max(1, w // 3), max(1, h // 3)
    noise = Image.new("L", (nw, nh))
    noise.putdata([rng.randint(0, 255) for _ in range(nw * nh)])
    noise = noise.resize((w, h), Image.Resampling.BILINEAR)
    noise_rgb = Image.merge("RGB", (noise, noise, noise))
    alpha = rng.uniform(0.08, 0.16)
    out = Image.blend(out, noise_rgb, alpha)
    out = ImageEnhance.Sharpness(out).enhance(rng.uniform(0.85, 1.15))
    return out


def _make_plant_overlay(size: tuple[int, int], rng: random.Random) -> Image.Image:
    """Procedural plant / leaf corner prop (no external asset required)."""
    w, h = size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    side = rng.choice(["bl", "br"])
    base_x = int(w * (0.08 if side == "bl" else 0.92))
    base_y = int(h * rng.uniform(0.72, 0.88))
    green = (rng.randint(34, 70), rng.randint(110, 160), rng.randint(60, 100), rng.randint(140, 200))
    stem = (40, 90, 55, 180)
    # Pot
    pot_w, pot_h = int(w * 0.11), int(h * 0.05)
    px0 = base_x - pot_w // 2
    d.rounded_rectangle(
        [px0, base_y, px0 + pot_w, base_y + pot_h],
        radius=8,
        fill=(120, 80, 60, 200),
    )
    # Stems + leaves
    for _ in range(rng.randint(5, 8)):
        tip_x = base_x + rng.randint(-int(w * 0.12), int(w * 0.12))
        tip_y = base_y - rng.randint(int(h * 0.08), int(h * 0.18))
        d.line([(base_x, base_y), (tip_x, tip_y)], fill=stem, width=rng.randint(3, 5))
        leaf_r = rng.randint(18, 36)
        d.ellipse(
            [tip_x - leaf_r, tip_y - leaf_r // 2, tip_x + leaf_r, tip_y + leaf_r // 2],
            fill=green,
        )
    # Soft light leak opposite corner
    leak = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(leak)
    lx = 0 if side == "br" else w
    color = (255, rng.randint(180, 220), rng.randint(140, 180), rng.randint(40, 70))
    ld.ellipse([lx - w // 2, -h // 5, lx + w // 2, h // 3], fill=color)
    leak = leak.filter(ImageFilter.GaussianBlur(radius=80))
    overlay = Image.alpha_composite(overlay, leak)
    return overlay


def _ai_augment_enabled() -> bool:
    """Opt-in gate: needs both FAL_KEY and CAROUSEL_AI_VARIATION=true in .env."""
    flag = os.getenv("CAROUSEL_AI_VARIATION", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    try:
        from image_ai_variation import fal_configured

        return fal_configured()
    except ImportError:
        return False


def _augment_ai(img: Image.Image, rng: random.Random) -> Image.Image:
    """
    AI remix via fal.ai (Ideogram V3, low strength) — subtly regenerates
    pixels/objects while keeping composition, on top of a color grade.
    Falls back to crop+grade if FAL_KEY isn't configured or the call fails,
    so a carousel render never breaks because of this.
    """
    import tempfile

    try:
        from image_ai_variation import ImageVariationError, remix_image_to_pil
    except ImportError:
        return _augment_grade(_augment_crop(img, rng), rng)

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        img.convert("RGB").save(tmp_path, format="PNG")
        remixed = remix_image_to_pil(tmp_path, strength=rng.uniform(0.16, 0.3))
        if remixed.size != img.size:
            remixed = ImageOps.fit(remixed, img.size, Image.Resampling.LANCZOS)
        return remixed
    except ImageVariationError:
        return _augment_grade(_augment_crop(img, rng), rng)
    except Exception:
        return _augment_grade(_augment_crop(img, rng), rng)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _augment_prop(img: Image.Image, rng: random.Random) -> Image.Image:
    """Add plant corner + optional light leak (keeps photo identity)."""
    base = img.convert("RGBA")
    custom = list(OVERLAYS_DIR.glob("*.png")) if OVERLAYS_DIR.is_dir() else []
    if custom and rng.random() < 0.5:
        prop = Image.open(rng.choice(custom)).convert("RGBA")
        # Scale prop to ~18% of width
        tw = int(img.size[0] * rng.uniform(0.14, 0.22))
        th = int(prop.size[1] * (tw / prop.size[0]))
        prop = prop.resize((tw, th), Image.Resampling.LANCZOS)
        x = rng.choice([20, img.size[0] - tw - 20])
        y = img.size[1] - th - rng.randint(40, 160)
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        layer.paste(prop, (x, y), prop)
        base = Image.alpha_composite(base, layer)
    else:
        base = Image.alpha_composite(base, _make_plant_overlay(img.size, rng))
    return base.convert("RGB")


def resolve_augment(method: str | None = None) -> str:
    """
    Resolve augment for a carousel.
    random (default) → pick one of crop|grade|grain|prop once for the whole
    set. "ai" only joins that random pool when CAROUSEL_AI_VARIATION=true
    and FAL_KEY are both set (see _ai_augment_enabled()); it can still be
    requested explicitly at any time (falls back gracefully if unconfigured).
    """
    m = (method or DEFAULT_AUGMENT).lower().strip()
    if m in ("random", "rand", "auto", ""):
        pool = list(AUGMENT_METHODS)
        if _ai_augment_enabled():
            pool.append("ai")
        return random.choice(pool)
    if m in ("none", "off"):
        return "none"
    if m == "ai":
        return "ai"
    if m in AUGMENT_METHODS:
        return m
    raise ValueError(
        f"Unknown augment '{method}'. Choose: {', '.join(AUGMENT_METHODS)}, ai, random, none"
    )


def apply_augment(
    img: Image.Image,
    method: str,
    seed: int | None = None,
) -> Image.Image:
    """Apply one augment method. method: crop|grade|grain|prop|ai|none."""
    method = (method or "none").lower().strip()
    if method in ("none", "", "off"):
        return img
    if method in ("random", "rand", "auto"):
        method = resolve_augment("random")
    if method != "ai" and method not in AUGMENT_METHODS:
        raise ValueError(
            f"Unknown augment '{method}'. Choose: {', '.join(AUGMENT_METHODS)}, ai, none"
        )
    rng = random.Random(seed if seed is not None else random.randint(0, 10_000_000))
    fn = {
        "crop": _augment_crop,
        "grade": _augment_grade,
        "grain": _augment_grain,
        "prop": _augment_prop,
        "ai": _augment_ai,
    }[method]
    return fn(img, rng)


def _label_strip(img: Image.Image, label: str) -> Image.Image:
    """Top label bar for A/B contact sheet."""
    bar_h = 72
    out = Image.new("RGB", (img.size[0], img.size[1] + bar_h), (20, 20, 20))
    out.paste(img, (0, bar_h))
    d = ImageDraw.Draw(out)
    font = get_tiktok_font(36)
    d.text((24, 16), label, fill=(255, 255, 255), font=font)
    return out


def run_ab_test(
    source: Path | None = None,
    seed: int = 42,
) -> list[Path]:
    """
    Generate original + 4 method variants of the SAME photo for A/B comparison.
    Also builds a 2x2 contact sheet of the 4 methods.
    """
    AB_TEST_DIR.mkdir(parents=True, exist_ok=True)
    photos = _collect_photos(None)
    avatars = _collect_images(AVATARS_DIR)
    pool = photos or avatars
    if source is not None:
        src_path = Path(source)
    elif pool:
        src_path = pool[0]
    else:
        raise FileNotFoundError(
            "No source photo. Drop a JPG in assets/photos/study_work/ or pass --source"
        )

    base = _load_and_cover(src_path, CANVAS_W, CANVAS_H)
    saved: list[Path] = []

    original_path = AB_TEST_DIR / "00_original.png"
    base.save(original_path, quality=95)
    saved.append(original_path)
    print(f"  Original: {original_path.name}  (from {src_path.name})")

    variants: list[Image.Image] = []
    for i, method in enumerate(AUGMENT_METHODS, start=1):
        # Same seed base + method offset → reproducible, still different per method
        aug = apply_augment(base, method, seed=seed + i * 17)
        labeled = _label_strip(aug, f"{i}. {method.upper()}")
        path = AB_TEST_DIR / f"{i:02d}_{method}.png"
        labeled.save(path, quality=95)
        saved.append(path)
        variants.append(labeled)
        print(f"  Method {method}: {path.name}")

    # 2x2 contact sheet (half-res thumbs)
    tw, th = CANVAS_W // 2, (CANVAS_H + 72) // 2
    sheet = Image.new("RGB", (tw * 2, th * 2), (0, 0, 0))
    for idx, var in enumerate(variants):
        thumb = var.resize((tw, th), Image.Resampling.LANCZOS)
        sheet.paste(thumb, ((idx % 2) * tw, (idx // 2) * th))
    sheet_path = AB_TEST_DIR / "ab_contact_sheet.png"
    sheet.save(sheet_path, quality=95)
    saved.append(sheet_path)
    print(f"  Contact sheet: {sheet_path.name}")
    print(f"A/B folder: {AB_TEST_DIR}")
    return saved


def _load_cover_augmented(
    path: Path,
    w: int,
    h: int,
    augment: str = "none",
    seed: int | None = None,
) -> Image.Image:
    img = _load_and_cover(path, w, h)
    return apply_augment(img, augment, seed=seed)


def _forced_segments(text: str) -> list[tuple[str, bool]]:
    """Split on | for manual breaks. !! prefix = white text on pink for that line."""
    parts = text.split("|") if "|" in text else [text]
    out: list[tuple[str, bool]] = []
    for segment in parts:
        segment = segment.strip()
        if not segment:
            continue
        white = segment.startswith("!!")
        if white:
            segment = segment[2:].strip()
        if segment:
            out.append((segment, white))
    return out


def _wrap_block(text: str, font, emoji_size: int, max_width: int) -> list[tuple[str, bool]]:
    """Forced lines (|) then word-wrap each segment. Keeps !! white-text flags."""
    out: list[tuple[str, bool]] = []
    for segment, white in _forced_segments(text):
        for line in _wrap_lines(segment, font, emoji_size, max_width):
            out.append((line, white))
    return out if out else [(text.replace("!!", "").strip(), False)]


def _wrap_lines(text: str, font, emoji_size: int, max_width: int) -> list[str]:
    """Word-wrap text to fit max_width (accounts for Apple emoji width)."""
    words = text.split()
    if not words:
        return [text]
    lines: list[str] = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        w, _ = _measure_mixed(test, font, emoji_size)
        if w <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines if lines else [text]


_CHOICE_RE = re.compile(r"\{\{([^{}]+)\}\}")


def _apply_variations(text: str) -> str:
    """Resolve {{a|b|c}} tokens — ~10–20% wording change per generation."""
    def pick(m: re.Match[str]) -> str:
        options = [o.strip() for o in m.group(1).split("|") if o.strip()]
        return random.choice(options) if options else ""
    return _CHOICE_RE.sub(pick, text)


def _caption_fields(item: tuple) -> tuple[str, bool, bool, bool]:
    """(text, emphasis, pin_top, small). Older 2-tuples still work."""
    text = item[0]
    emphasis = bool(item[1]) if len(item) > 1 else False
    pin_top = bool(item[2]) if len(item) > 2 else False
    small = bool(item[3]) if len(item) > 3 else False
    return text, emphasis, pin_top, small


def _parse_caption_block(raw: str) -> list[tuple[str, bool, bool, bool]]:
    """
    Prefixes, in any order, before the text:
      ^  pin this pâté to the top (rest stays centered below)
      ~  small badge
      !  white box / black text (not !! white-on-color)
    """
    lines: list[tuple[str, bool, bool, bool]] = []
    for ln in raw.strip().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        pin_top = False
        small = False
        while ln[:1] in "^~":
            if ln.startswith("^"):
                pin_top = True
            else:
                small = True
            ln = ln[1:].strip()
        emphasis = False
        if ln.startswith("!") and not ln.startswith("!!"):
            emphasis = True
            ln = ln[1:].strip()
        if not ln:
            continue
        lines.append((_apply_variations(ln), emphasis, pin_top, small))
    return lines


def _split_caption_sections(content: str) -> dict[str, list[str]]:
    """Parse === hooks === / === method === sections (default bucket = hooks)."""
    sections: dict[str, list[str]] = {"hooks": [], "method": []}
    current = "hooks"
    buf: list[str] = []

    def flush():
        nonlocal buf
        block = "\n".join(buf).strip()
        if block:
            sections.setdefault(current, []).append(block)
        buf = []

    for ln in content.splitlines():
        m = re.match(r"^===\s*(\w+)\s*===\s*$", ln.strip(), re.I)
        if m:
            flush()
            current = m.group(1).lower()
            sections.setdefault(current, [])
            continue
        if ln.strip() == "---":
            flush()
            continue
        buf.append(ln)
    flush()
    return sections


def _load_caption_pools() -> dict[str, list[list[tuple[str, bool]]]]:
    """Load caption pools by role. Variations are applied when parsing a pick."""
    if not CAPTIONS_FILE.exists():
        return {"hooks": [REFERENCE_CAPTION], "method": [REFERENCE_CAPTION]}
    content = CAPTIONS_FILE.read_text(encoding="utf-8")
    if "===" not in content:
        # Legacy flat file: all sets = hooks
        blocks = [b.strip() for b in content.split("---") if b.strip()]
        sets = [_parse_caption_block(b) for b in blocks]
        sets = [s for s in sets if s] or [REFERENCE_CAPTION]
        return {"hooks": sets, "method": sets}
    raw_sections = _split_caption_sections(content)
    pools: dict[str, list[list[tuple[str, bool]]]] = {}
    for role, blocks in raw_sections.items():
        parsed = [_parse_caption_block(b) for b in blocks]
        pools[role] = [s for s in parsed if s]
    if not pools.get("hooks"):
        pools["hooks"] = [REFERENCE_CAPTION]
    if not pools.get("method"):
        pools["method"] = pools["hooks"]
    return pools


def _block_style(raw: str) -> str:
    """Read `# style: routine|etape` from a caption block (default: routine)."""
    for ln in raw.splitlines():
        m = re.match(r"^#\s*style\s*:\s*(\w+)\s*$", ln.strip(), re.I)
        if m:
            return m.group(1).lower()
    # Heuristic fallback
    low = raw.lower()
    if "#étape" in low or "#etape" in low:
        return "etape"
    return "routine"


def _blocks_by_style(role: str) -> dict[str, list[str]]:
    """Group raw caption blocks for a role by style tag."""
    if not CAPTIONS_FILE.exists():
        return {"routine": []}
    content = CAPTIONS_FILE.read_text(encoding="utf-8")
    sections = _split_caption_sections(content)
    out: dict[str, list[str]] = {}
    for block in sections.get(role, []):
        style = _block_style(block)
        out.setdefault(style, []).append(block)
    return out


def _block_has_text(raw: str) -> bool:
    for ln in raw.strip().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            return True
    return False


def _pick_caption_set(role: str = "hooks", style: str | None = None) -> list[tuple[str, bool]]:
    """Pick a template for role (optionally filtered by style). Fresh {{…}} each time."""
    if not CAPTIONS_FILE.exists():
        return list(REFERENCE_CAPTION)
    content = CAPTIONS_FILE.read_text(encoding="utf-8")
    if "===" not in content:
        blocks = [b.strip() for b in content.split("---") if b.strip()]
        usable = [b for b in blocks if _block_has_text(b)]
        return _parse_caption_block(random.choice(usable)) if usable else list(REFERENCE_CAPTION)
    if style:
        by_style = _blocks_by_style(role)
        blocks = by_style.get(style) or by_style.get("routine") or []
        for other in by_style.values():
            if other:
                blocks = blocks or other
                break
    else:
        sections = _split_caption_sections(content)
        blocks = sections.get(role) or sections.get("hooks") or []
    usable = [b for b in blocks if _block_has_text(b)]
    if not usable:
        return list(REFERENCE_CAPTION)
    return _parse_caption_block(random.choice(usable))


def _collect_mix_photos() -> list[Path]:
    """Every photo pack except dedicated backgrounds (dark cities)."""
    if not PHOTOS_DIR.is_dir():
        return []
    photos: list[Path] = []
    for child in sorted(PHOTOS_DIR.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_dir():
            if child.name in PHOTO_PACKS_EXCLUDED_FROM_MIX:
                continue
            photos.extend(_collect_images(child))
            continue
        if child.suffix.lower() in _IMG_EXTS and not child.name.lower().startswith("readme"):
            photos.append(child)
    return photos


def _complete_caption_styles() -> list[str]:
    """Pâté families that have method + JobShift + recap."""
    method_by = _blocks_by_style("method")
    jellyjob_by = _blocks_by_style("jellyjob")
    recap_by = _blocks_by_style("recap")
    common = set(method_by) & set(jellyjob_by) & set(recap_by)
    order = ("routine", "etape", "template2")
    styles = [s for s in order if s in common]
    styles += [s for s in common if s not in styles]
    if not styles:
        styles = [s for s in method_by if method_by[s]] or ["routine"]
    return styles


def _pick_paired_slides(forced_style: str | None = None) -> tuple[
    str,
    list[tuple[str, bool]],
    list[tuple[str, bool]],
    list[tuple[str, bool]],
]:
    """
    Pick one coherent style for slides 2–4:
      routine  → Google 8h00–8h10 + JobShift 8h10–8h15 + recap 8h15–8h20
      etape    → #étape 1# + #étape 2# + récap blanc
      template2 → #1# Google Emploi + #2# JobShift + récap rentrée
    """
    styles = _complete_caption_styles()
    if forced_style == "template2" and "template2" in styles:
        styles = ["template2"]
    elif forced_style == "template1":
        classic = [s for s in styles if s in ("routine", "etape")]
        styles = classic or styles
    style = random.choice(styles)
    method = _pick_caption_set("method", style=style)
    jellyjob = _pick_caption_set("jellyjob", style=style)
    recap = _pick_caption_set("recap", style=style)
    return style, method, jellyjob, recap


def _pick_font_size(text: str) -> int:
    """Short text → large font (fills more space); long text → smaller."""
    plain = _CHOICE_RE.sub(lambda m: m.group(1).split("|")[0], text)
    plain = plain.replace("|", " ").replace("!!", "").strip()
    n = len(plain)
    if n <= 18:
        return FONT_SIZE_MAX
    if n <= 35:
        return 62
    if n <= 55:
        return FONT_SIZE_BASE
    if n <= 90:
        return 44
    return FONT_SIZE_MIN


def _layout_blocks(
    caption_lines: list[tuple[str, bool]],
) -> list[dict]:
    """
    TikTok pâté: each line hugs its text with uniform pad (like the reference).
    Continuous surface with concave corners at width steps. All centered.
    """
    blocks: list[dict] = []
    num = len(caption_lines)

    for block_idx, item in enumerate(caption_lines):
        text, emphasis, pin_top, small = _caption_fields(item)
        size = BADGE_FONT_SIZE if small else _pick_font_size(text)
        font = get_tiktok_font(size)
        inner_max = MAX_TEXT_WIDTH - PILL_PAD_X * 2
        wrapped = _wrap_block(text, font, size, inner_max)
        if not wrapped:
            wrapped = [((text or " ").strip() or " ", False)]

        while True:
            line_sizes = [_measure_mixed(ln, font, size) for ln, _ in wrapped]
            max_lw = max(w for w, _ in line_sizes)
            if max_lw <= inner_max and len(wrapped) <= 5:
                break
            if size <= FONT_SIZE_MIN:
                break
            size -= 2
            font = get_tiktok_font(size)
            wrapped = _wrap_block(text, font, size, inner_max)
            line_sizes = [_measure_mixed(ln, font, size) for ln, _ in wrapped]

        bands: list[dict] = []
        for (line, white_text), (lw, lh) in zip(wrapped, line_sizes):
            bw = min(MAX_TEXT_WIDTH, lw + PILL_PAD_X * 2)
            bh = lh + PILL_PAD_Y * 2
            bands.append({
                "text": line,
                "white_text": white_text,
                "lw": lw,
                "lh": lh,
                "bw": bw,
                "bh": bh,
                "bx": (CANVAS_W - bw) // 2,
            })

        total_bh = sum(b["bh"] for b in bands) - SEAM_OVERLAP * max(0, len(bands) - 1)
        gap_after = (
            random.randint(BLOCK_GAP_MIN, BLOCK_GAP_MAX)
            if block_idx < num - 1
            else 0
        )

        blocks.append({
            "bands": bands,
            "emphasis": emphasis,
            "font": font,
            "emoji_size": size,
            "bh": total_bh,
            "gap_after": gap_after,
            "pin_top": pin_top,
        })

    return blocks


def _draw_pate_mask(bands: list[dict], y_start: int, radius: int) -> tuple[Image.Image, list[int]]:
    """
    Clean stepped pâté: solid pink, round outer corners only.
    No black scoops / notches cut into the fill.
    """
    mask = Image.new("L", (CANVAS_W, CANVAS_H), 0)
    md = ImageDraw.Draw(mask)
    r = min(radius, max(4, min(b["bh"] for b in bands) // 2))
    n = len(bands)
    band_ys: list[int] = []
    rects: list[tuple[int, int, int, int]] = []

    y = y_start
    for i, band in enumerate(bands):
        band_ys.append(y)
        y0 = y
        y1 = y + band["bh"]
        x0, x1 = band["bx"], band["bx"] + band["bw"]
        rects.append((x0, y0, x1, y1))
        # Fully rounded pill per line — next line starts higher so they merge
        md.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=255)
        if i < n - 1:
            y = y1 - SEAM_OVERLAP

    # Soft bridge on shared width only (keeps shape solid, no black gaps in the middle)
    for i in range(n - 1):
        ax0, _, ax1, ay1 = rects[i]
        bx0, by0, bx1, _ = rects[i + 1]
        mx0, mx1 = max(ax0, bx0), min(ax1, bx1)
        if mx1 > mx0:
            md.rectangle([mx0, min(ay1, by0) - 2, mx1, max(ay1, by0) + 2], fill=255)

    return mask, band_ys


def _stack_height(blocks: list[dict]) -> int:
    return sum(b["bh"] + b["gap_after"] for b in blocks)


def _scale_blocks(blocks: list[dict], scale: float) -> None:
    for b in blocks:
        for band in b["bands"]:
            band["bh"] = max(28, int(band["bh"] * scale))
        b["bh"] = sum(band["bh"] for band in b["bands"]) - SEAM_OVERLAP * max(0, len(b["bands"]) - 1)
        if b["gap_after"] > 0:
            b["gap_after"] = max(10, int(b["gap_after"] * scale))


def _paint_blocks(
    overlay: Image.Image,
    blocks: list[dict],
    start_y: int,
    style: dict,
) -> tuple[Image.Image, int]:
    cy = start_y
    for b in blocks:
        if b["emphasis"]:
            bg = style["emphasis_bg"] + (255,)
            fg = style["emphasis_text"] + (255,)
        else:
            bg = style["box_bg"] + (255,)
            fg = style["box_text"] + (255,)

        mask, band_ys = _draw_pate_mask(b["bands"], cy, PILL_RADIUS)

        color_layer = Image.new("RGBA", overlay.size, bg)
        overlay = Image.composite(color_layer, overlay, mask)

        od = ImageDraw.Draw(overlay)
        for i, band in enumerate(b["bands"]):
            y = band_ys[i]
            tx = band["bx"] + (band["bw"] - band["lw"]) // 2
            ty = y + PILL_PAD_Y  # identical top margin for pink + white titles
            line_fg = fg
            if not b["emphasis"] and band.get("white_text"):
                line_fg = style["highlight_text"] + (255,)
            _draw_mixed_text(
                overlay, od, (tx, ty), band["text"],
                b["font"], b["emoji_size"], line_fg,
            )

        cy += b["bh"] + b["gap_after"]
    return overlay, cy


def draw_stacked_text_boxes(
    img: Image.Image,
    caption_lines: list[tuple],
    style: dict,
) -> Image.Image:
    """TikTok pâté: uniform pad, tight lines, round corners, centered — no artifacts.

    A pâté marked pin_top (caption prefix ^) stays near the top. The rest is
    centered in the space below it.
    """
    if not caption_lines:
        return img

    blocks = _layout_blocks(caption_lines)
    pinned = [b for b in blocks if b.get("pin_top")]
    body = [b for b in blocks if not b.get("pin_top")]

    usable_top = int(CANVAS_H * TOP_MARGIN)
    usable_bottom = int(CANVAS_H * (1 - BOTTOM_MARGIN))
    usable_h = usable_bottom - usable_top
    split_gap = int(CANVAS_H * 0.03) if pinned and body else 0

    total_h = _stack_height(pinned) + _stack_height(body) + split_gap
    if total_h > usable_h:
        scale = usable_h / total_h
        _scale_blocks(blocks, scale)
        split_gap = max(8, int(split_gap * scale))

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    if pinned:
        overlay, pin_end = _paint_blocks(overlay, pinned, usable_top, style)
        body_top = pin_end + split_gap
    else:
        body_top = usable_top

    body_h = _stack_height(body)
    if pinned:
        # Badge stays up top; the hook sits in the lower area, like the référence.
        start_y = max(body_top, usable_bottom - body_h)
    else:
        body_room = max(0, usable_bottom - body_top)
        start_y = body_top + max(0, (body_room - body_h) // 2)
    overlay, _ = _paint_blocks(overlay, body, start_y, style)

    base = img.convert("RGBA")
    return Image.alpha_composite(base, overlay).convert("RGB")


def render_slide_with_text(
    bg: Image.Image,
    caption_lines: list[tuple[str, bool]] | None,
    style: dict,
) -> Image.Image:
    img = bg.copy()
    if caption_lines:
        img = draw_stacked_text_boxes(img, caption_lines, style)
    return img


def _caption_preview(lines: list[tuple[str, bool]]) -> str:
    parts: list[str] = []
    for item in lines[:2]:
        text, _emphasis, _pin, _small = _caption_fields(item)
        clean = text.replace("|", " ").replace("!!", "").replace("!", "").strip()
        if clean:
            parts.append(clean[:48])
    return " · ".join(parts) if parts else "hook"


HOOK_BLUE = (45, 88, 250)
TEMPLATE3_COUNTS = (3, 4, 5)


def _vary(text: str) -> str:
    return _apply_variations(text).strip()


def _load_template3() -> dict | None:
    if not TEMPLATE3_FILE.is_file():
        return None
    try:
        data = json.loads(TEMPLATE3_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not data.get("companies"):
        return None
    return data


def _template3_available() -> bool:
    data = _load_template3()
    if not data:
        return False
    return bool(_collect_images(PHOTOS_DIR / DARK_CITIES_PACK))


def _wants_template3(
    caption_lines,
    method_lines,
    jellyjob_lines,
    recap_lines,
) -> bool:
    """Template 3 enters the rotation only once night-city photos exist."""
    if any(
        value is not None
        for value in (caption_lines, method_lines, jellyjob_lines, recap_lines)
    ):
        return False
    if not _template3_available():
        return False
    families = _complete_caption_styles() + ["template3"]
    return random.choice(families) == "template3"


def _draw_mixed_stroked(
    overlay: Image.Image,
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font,
    emoji_size: int,
    fill: tuple,
    stroke: tuple = (0, 0, 0, 255),
    radius: int = 2,
) -> None:
    """White text with a dark outline. Emoji are pasted once, not stacked."""
    x, y = xy
    line_h = _measure_mixed(text, font, emoji_size)[1]
    offsets = ((-2, 0), (2, 0), (0, -2), (0, 2), (-2, -2), (2, -2), (-2, 2), (2, 2))

    cursor = x
    for part, is_emoji in _split_text_runs(text):
        if is_emoji:
            em = _load_apple_emoji(part, emoji_size)
            if em is not None:
                cursor += em.size[0]
            else:
                cursor += emoji_size
            continue
        left, top, right, bottom = _text_bbox(part, font)
        tw = right - left
        th = bottom - top
        ty = y + (line_h - th) // 2 - top
        tx = cursor - left
        for dx, dy in offsets:
            draw.text((tx + dx, ty + dy), part, font=font, fill=stroke)
        draw.text((tx, ty), part, font=font, fill=fill)
        cursor += tw

    cursor = x
    for part, is_emoji in _split_text_runs(text):
        if not is_emoji:
            left, _top, right, _bottom = _text_bbox(part, font)
            cursor += right - left
            continue
        em = _load_apple_emoji(part, emoji_size)
        if em is None:
            cursor += emoji_size
            continue
        ey = y + (line_h - em.size[1]) // 2
        overlay.paste(em, (cursor, ey), em)
        cursor += em.size[0]


def _line_size(text: str, font, emoji_size: int) -> tuple[int, int]:
    return _measure_mixed(text, font, emoji_size)


def _fit_font(text: str, max_width: int, start: int, minimum: int = 36):
    size = start
    font = get_tiktok_font(size)
    while size > minimum and _line_size(text, font, size)[0] > max_width:
        size -= 2
        font = get_tiktok_font(size)
    return font, size


def _draw_centered_block(
    overlay: Image.Image,
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font,
    emoji_size: int,
    y: int,
    fill: tuple,
    *,
    stroke: bool = False,
    line_gap: int = 8,
    align: str = "center",
) -> int:
    """Draw a centered block. align=left keeps a shared left edge, still centered as a group."""
    if not lines:
        return y
    sizes = [_line_size(line, font, emoji_size) for line in lines]
    block_w = max(w for w, _ in sizes)
    x0 = (CANVAS_W - block_w) // 2
    cy = y
    for line, (lw, lh) in zip(lines, sizes):
        x = x0 if align == "left" else x0 + (block_w - lw) // 2
        if stroke:
            _draw_mixed_stroked(overlay, draw, (x, cy), line, font, emoji_size, fill)
        else:
            _draw_mixed_text(overlay, draw, (x, cy), line, font, emoji_size, fill)
        cy += lh + line_gap
    return cy


def _draw_pill(
    overlay: Image.Image,
    text_lines: list[str],
    font,
    y: int,
    bg: tuple,
    fg: tuple,
    *,
    pad_x: int = 36,
    pad_y: int = 16,
    gap: int = 4,
) -> int:
    emoji_size = font.size if hasattr(font, "size") else 48
    sizes = [_line_size(line, font, emoji_size) for line in text_lines]
    text_w = max(w for w, _ in sizes)
    text_h = sum(h for _, h in sizes) + gap * max(0, len(sizes) - 1)
    box_w = text_w + pad_x * 2
    box_h = text_h + pad_y * 2
    x0 = (CANVAS_W - box_w) // 2
    draw = ImageDraw.Draw(overlay)
    radius = min(28, box_h // 2)
    draw.rounded_rectangle([x0, y, x0 + box_w, y + box_h], radius=radius, fill=bg + (255,))
    cy = y + pad_y
    for line, (lw, lh) in zip(text_lines, sizes):
        tx = x0 + (box_w - lw) // 2
        _draw_mixed_text(overlay, draw, (tx, cy), line, font, emoji_size, fg + (255,))
        cy += lh + gap
    return y + box_h


def _render_template3_hook(bg: Image.Image, count: int, spec: dict) -> Image.Image:
    overlay = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    title_font, _ = _fit_font(f"{count} entreprises", 900, 86, 64)
    sub_lines = [_vary(line) for line in spec.get("lines", ["où faire ton alternance", "en 2026"])]
    sub_font, _ = _fit_font(max(sub_lines, key=len), 920, 54, 40)
    y = int(CANVAS_H * 0.22)
    y = _draw_pill(
        overlay,
        [f"{count} entreprises"],
        title_font,
        y,
        HOOK_BLUE,
        (255, 255, 255),
        pad_x=42,
        pad_y=18,
    )
    y += 18
    _draw_pill(
        overlay,
        sub_lines,
        sub_font,
        y,
        (255, 255, 255),
        (17, 17, 17),
        pad_x=34,
        pad_y=16,
    )
    return Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")


def _wrap_benefit(text: str, font, emoji_size: int, max_width: int) -> list[str]:
    wrapped = _wrap_lines(text, font, emoji_size, max_width) or [text]
    if len(wrapped) == 1:
        return wrapped
    cont = []
    for extra in wrapped[1:]:
        if extra and not _is_emoji_char(extra[0]):
            cont.append("    " + extra)
        else:
            cont.append(extra)
    return [wrapped[0], *cont]


def _render_template3_company(bg: Image.Image, company: dict) -> Image.Image:
    name = _vary(company.get("name", ""))
    role = _vary(company.get("role", ""))
    raw_lines = [_vary(line) for line in company.get("lines", []) if str(line).strip()]
    overlay = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    name_font, name_size = _fit_font(name, 880, 72, 48)
    name_w, name_h = _line_size(name, name_font, name_size)
    pad_x, pad_y = 56, 32
    card_w = max(name_w + pad_x * 2, 620)
    card_h = name_h + pad_y * 2
    card_x = (CANVAS_W - card_w) // 2
    card_y = int(CANVAS_H * 0.16)
    draw.rounded_rectangle(
        [card_x, card_y, card_x + card_w, card_y + card_h],
        radius=16,
        fill=(255, 255, 255, 255),
    )
    _draw_mixed_text(
        overlay,
        draw,
        (card_x + (card_w - name_w) // 2, card_y + pad_y),
        name,
        name_font,
        name_size,
        (20, 20, 20, 255),
    )

    y = card_y + card_h + 36
    max_w = CANVAS_W - 100
    role_font, role_size = _fit_font(role, max_w, 46, 34)
    if role:
        y = _draw_centered_block(
            overlay, draw, [role], role_font, role_size, y,
            (255, 255, 255, 255), stroke=True, line_gap=6,
        )
        y += 16

    body_font = get_tiktok_font(44)
    body_size = 44
    lines: list[str] = []
    for raw in raw_lines:
        lines.extend(_wrap_benefit(raw, body_font, body_size, max_w))

    text_h = sum(_line_size(line, body_font, body_size)[1] + 12 for line in lines)
    room = int(CANVAS_H * 0.92) - y
    if lines and text_h > room and room > 0:
        scale = max(0.72, room / text_h)
        body_size = max(32, int(44 * scale))
        body_font = get_tiktok_font(body_size)
        lines = []
        for raw in raw_lines:
            lines.extend(_wrap_benefit(raw, body_font, body_size, max_w))

    _draw_centered_block(
        overlay, draw, lines, body_font, body_size, y,
        (255, 255, 255, 255), stroke=True, line_gap=12, align="left",
    )
    return Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")


def _render_template3_cta(bg: Image.Image, spec: dict) -> Image.Image:
    overlay = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    question = [_vary(line) for line in spec.get("question", [])]
    box_lines = [_vary(line) for line in spec.get("box", [])]
    q_font, q_size = _fit_font(max(question or ["?"], key=len), 960, 54, 40)
    y = int(CANVAS_H * 0.20)
    y = _draw_centered_block(
        overlay, draw, question, q_font, q_size, y,
        (255, 255, 255, 255), stroke=True, line_gap=10,
    )
    y += 28
    box_font, _ = _fit_font(max(box_lines or ["."], key=len), 900, 42, 32)
    _draw_pill(
        overlay,
        box_lines,
        box_font,
        y,
        (255, 255, 255),
        (17, 17, 17),
        pad_x=32,
        pad_y=18,
        gap=8,
    )
    return Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")


def _pick_template3_companies(data: dict) -> tuple[int, list[dict]]:
    companies = list(data.get("companies") or [])
    allowed = data.get("company_counts") or list(TEMPLATE3_COUNTS)
    allowed = [int(n) for n in allowed if int(n) >= 1]
    if not allowed:
        allowed = [3, 4, 5]
    count = random.choice(allowed)
    count = min(count, len(companies))
    picked = companies[:]
    random.shuffle(picked)
    return count, picked[:count]


def _generate_template3(
    dest: Path,
    *,
    set_index: int = 0,
    augment: str = DEFAULT_AUGMENT,
    quiet: bool = False,
    output_dir: Path | None = None,
) -> CarouselBuild:
    data = _load_template3()
    if not data:
        raise FileNotFoundError(f"Template 3 introuvable : {TEMPLATE3_FILE}")
    photos = _collect_images(PHOTOS_DIR / DARK_CITIES_PACK)
    count, companies = _pick_template3_companies(data)
    aug = resolve_augment(augment)
    seed_base = random.randint(0, 1_000_000)
    prefix = "" if output_dir is not None else f"carousel_{set_index:02d}_"
    pool = photos[:]
    random.shuffle(pool)
    slide_total = count + 2  # hook + companies + cta

    def _bg(index: int) -> Image.Image:
        if not pool:
            return Image.new("RGB", (CANVAS_W, CANVAS_H), (8, 10, 18))
        path = pool[index % len(pool)]
        img = _load_cover_augmented(path, CANVAS_W, CANVAS_H, aug, seed_base + index)
        return ImageEnhance.Brightness(img).enhance(0.9)

    def _log(message: str) -> None:
        if not quiet:
            print(message)

    saved: list[Path] = []
    hook = _render_template3_hook(_bg(0), count, data.get("hook") or {})
    path0 = dest / f"{prefix}slide_00.png"
    hook.save(path0, quality=95)
    saved.append(path0)
    names = [pool[0].name] if pool else ["fond-sombre"]
    _log(f"  Slide 1/{slide_total}: {path0.name}  (HOOK alternance, {count} entreprises)")

    for i, company in enumerate(companies):
        img = _render_template3_company(_bg(i + 1), company)
        path = dest / f"{prefix}slide_{i + 1:02d}.png"
        img.save(path, quality=95)
        saved.append(path)
        names.append(pool[(i + 1) % len(pool)].name if pool else "fond-sombre")
        _log(f"  Slide {i + 2}/{slide_total}: {path.name}  ({company.get('name', '')})")

    cta = _render_template3_cta(_bg(count + 1), data.get("cta") or {})
    path_cta = dest / f"{prefix}slide_{count + 1:02d}.png"
    cta.save(path_cta, quality=95)
    saved.append(path_cta)
    names.append(pool[(count + 1) % len(pool)].name if pool else "fond-sombre")
    _log(f"  Slide {slide_total}/{slide_total}: {path_cta.name}  (CTA commentaires)")
    _log(f"  Caption style pair: template3  |  entreprises: {count}  |  augment: {aug}")

    hook_preview = f"{count} entreprises"
    return CarouselBuild(
        slides=saved,
        pair_style="template3",
        augment=aug,
        color="city",
        avatar_name=DARK_CITIES_PACK,
        photo_names=names,
        hook_preview=hook_preview,
    )


def _load_template4() -> dict | None:
    if not TEMPLATE4_FILE.is_file():
        return None
    try:
        data = json.loads(TEMPLATE4_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not data.get("sectors"):
        return None
    return data


def _template4_available() -> bool:
    return _load_template4() is not None


def _pick_extra_template(
    caption_lines,
    method_lines,
    jellyjob_lines,
    recap_lines,
) -> str | None:
    """None keeps the classic carousel. template3 / template4 join that rotation."""
    if any(
        value is not None
        for value in (caption_lines, method_lines, jellyjob_lines, recap_lines)
    ):
        return None
    choices = ["type1"] * max(1, len(_complete_caption_styles()))
    if _template3_available():
        choices.append("template3")
    if _template4_available():
        choices.append("template4")
    pick = random.choice(choices)
    return None if pick == "type1" else pick


def _company_line(company: dict) -> str:
    name = str(company.get("name") or "").strip()
    note = str(company.get("note") or "").strip()
    if note and random.random() < 0.55:
        return f"{name} ({note})"
    return name


def _sample_template4_sectors(data: dict) -> list[dict]:
    bounds = data.get("per_sector") or {}
    low = int(bounds.get("min") or 3)
    high = int(bounds.get("max") or 6)
    sectors = list(data.get("sectors") or [])
    random.shuffle(sectors)
    if len(sectors) > 5 and random.random() < 0.35:
        sectors = sectors[:-1]
    sampled: list[dict] = []
    for sector in sectors:
        companies = [c for c in (sector.get("companies") or []) if c.get("name")]
        if not companies:
            continue
        random.shuffle(companies)
        take = random.randint(min(low, len(companies)), min(max(low, high), len(companies)))
        lines = [_company_line(company) for company in companies[:take]]
        sampled.append({"title": str(sector.get("title") or "").strip(), "lines": lines})
    return [block for block in sampled if block["title"] and block["lines"]]


def _notes_text_size(text: str, font) -> tuple[int, int]:
    left, top, right, bottom = font.getbbox(text)
    return right - left, bottom - top


def _wrap_plain(text: str, font, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return [text]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if _notes_text_size(trial, font)[0] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _template4_slide_blocks(sectors: list[dict], title_font, body_font, max_width: int) -> list[dict]:
    blocks = []
    for sector in sectors:
        bullets: list[str] = []
        for line in sector["lines"]:
            bullets.extend(_wrap_plain(f"•  {line}", body_font, max_width))
        blocks.append({"title": sector["title"], "bullets": bullets})
    return blocks


def _template4_slide_height(blocks: list[dict], title_font, body_font) -> int:
    title_h = _notes_text_size("Ag", title_font)[1]
    body_h = _notes_text_size("Ag", body_font)[1]
    total = 0
    for index, block in enumerate(blocks):
        if index:
            total += 44
        total += title_h + 16
        total += len(block["bullets"]) * (body_h + 14)
    return total


def _pack_template4_slides(sectors: list[dict]) -> list[list[dict]]:
    """Spread sectors across the 3 black Notes slides, then trim so text stays on canvas."""
    buckets: list[list[dict]] = [[], [], []]
    for index, sector in enumerate(sectors):
        buckets[index % 3].append(sector)
    title_font = get_tiktok_font(46)
    body_font = get_tiktok_font(36)
    max_width = CANVAS_W - 150
    budget = 1580
    packed: list[list[dict]] = []
    for bucket in buckets:
        if not bucket:
            continue
        while bucket and _template4_slide_height(
            _template4_slide_blocks(bucket, title_font, body_font, max_width),
            title_font,
            body_font,
        ) > budget:
            longest = max(bucket, key=lambda item: len(item["lines"]))
            if len(longest["lines"]) > 2:
                longest["lines"] = longest["lines"][:-1]
            elif len(bucket) > 1:
                bucket.remove(longest)
            else:
                break
        packed.append(bucket)
    return packed[:3]


def _render_template4_hook(bg: Image.Image, lines: list[str]) -> Image.Image:
    overlay = Image.new("RGBA", bg.size, (0, 0, 0, 40))
    draw = ImageDraw.Draw(overlay)
    font, _size = _fit_font(max(lines, key=len), 980, 68, 42)
    heights = [_notes_text_size(line, font)[1] for line in lines]
    gap = 14
    total = sum(heights) + gap * max(0, len(lines) - 1)
    y = (CANVAS_H - total) // 2
    for line, height in zip(lines, heights):
        width, _ = _notes_text_size(line, font)
        x = (CANVAS_W - width) // 2
        draw.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0, 170))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += height + gap
    return Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")


def _render_template4_notes(sectors: list[dict]) -> Image.Image:
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    title_font = get_tiktok_font(46)
    body_font = get_tiktok_font(36)
    margin_x = 78
    max_width = CANVAS_W - margin_x - 64
    blocks = _template4_slide_blocks(sectors, title_font, body_font, max_width)
    y = 200
    title_h = _notes_text_size("Ag", title_font)[1]
    body_h = _notes_text_size("Ag", body_font)[1]
    for index, block in enumerate(blocks):
        if index:
            y += 44
        title = block["title"]
        title_w, _ = _notes_text_size(title, title_font)
        draw.text((margin_x, y), title, font=title_font, fill=(255, 255, 255))
        underline_y = y + title_h + 6
        draw.line(
            [(margin_x, underline_y), (margin_x + title_w, underline_y)],
            fill=(255, 255, 255),
            width=2,
        )
        y = underline_y + 16
        for bullet in block["bullets"]:
            draw.text((margin_x, y), bullet, font=body_font, fill=(255, 255, 255))
            y += body_h + 14
    return img


def _generate_template4(
    dest: Path,
    *,
    set_index: int = 0,
    avatar_pack: str = DEFAULT_AVATAR_PACK,
    photo_pack: str | None = DEFAULT_PHOTO_PACK,
    augment: str = DEFAULT_AUGMENT,
    quiet: bool = False,
    output_dir: Path | None = None,
) -> CarouselBuild:
    data = _load_template4()
    if not data:
        raise FileNotFoundError(f"Template 4 introuvable : {TEMPLATE4_FILE}")
    avatars = _collect_images(_avatar_pack_dir(avatar_pack))
    if not avatars:
        raise FileNotFoundError(
            f"Template 4 a besoin d'un avatar — dépose des fichiers dans {_avatar_pack_dir(avatar_pack)}"
        )

    hooks = [h for h in (data.get("hooks") or []) if h]
    hook_lines = list(random.choice(hooks)) if hooks else [
        "LES GRANDES ENTREPRISES",
        "QUI RECRUTENT MASSIVEMENT",
        "EN ALTERNANCE",
        "(2026)",
    ]
    sectors = _sample_template4_sectors(data)
    slides_sectors = _pack_template4_slides(sectors)
    aug = resolve_augment("none" if augment == "random" else augment)
    if aug in ("crop", "prop", "ai"):
        aug = "grade"
    photo = random.choice(avatars)
    seed_base = random.randint(0, 1_000_000)
    prefix = "" if output_dir is not None else f"carousel_{set_index:02d}_"
    bg = _load_cover_augmented(photo, CANVAS_W, CANVAS_H, aug, seed_base)
    bg = ImageEnhance.Brightness(bg).enhance(0.97)

    def _log(message: str) -> None:
        if not quiet:
            print(message)

    saved: list[Path] = []
    hook = _render_template4_hook(bg, hook_lines)
    path0 = dest / f"{prefix}slide_00.png"
    hook.save(path0, quality=95)
    saved.append(path0)
    _log(f"  Slide 1/4: {path0.name}  (HOOK template4, photo {photo.name})")

    for index, page in enumerate(slides_sectors, start=1):
        img = _render_template4_notes(page)
        path = dest / f"{prefix}slide_{index:02d}.png"
        img.save(path, quality=95)
        saved.append(path)
        page_titles = ", ".join(block["title"] for block in page)
        _log(f"  Slide {index + 1}/4: {path.name}  ({page_titles})")

    _log("  Caption style pair: template4  |  notes iPhone, fond noir")
    return CarouselBuild(
        slides=saved,
        pair_style="template4",
        augment=aug,
        color="notes",
        avatar_name=avatar_pack,
        photo_names=[photo.name],
        hook_preview=" ".join(hook_lines[:2]),
    )


def generate_type1_carousel(
    color: str = DEFAULT_COLOR,
    set_index: int = 0,
    caption_lines: list[tuple[str, bool]] | None = None,
    method_lines: list[tuple[str, bool]] | None = None,
    jellyjob_lines: list[tuple[str, bool]] | None = None,
    recap_lines: list[tuple[str, bool]] | None = None,
    avatar_pack: str = DEFAULT_AVATAR_PACK,
    photo_pack: str | None = DEFAULT_PHOTO_PACK,
    augment: str = DEFAULT_AUGMENT,
    output_dir: Path | None = None,
    quiet: bool = False,
    carousel_style: str | None = None,
) -> CarouselBuild:
    """
    Type 1 (strict):
      slide 1 = AVATAR + hook
      slide 2 = STYLE + Google (routine OR etape)
      slide 3 = STYLE + JobShift (same style)
      slide 4 = STYLE + recap (same style)
    """
    dest = Path(output_dir) if output_dir is not None else OUTPUT_DIR
    dest.mkdir(parents=True, exist_ok=True)

    if carousel_style == "template3":
        return _generate_template3(
            dest,
            set_index=set_index,
            augment=augment,
            quiet=quiet,
            output_dir=output_dir,
        )
    if carousel_style == "template4":
        return _generate_template4(
            dest,
            set_index=set_index,
            avatar_pack=avatar_pack,
            photo_pack=photo_pack,
            augment=augment,
            quiet=quiet,
            output_dir=output_dir,
        )

    extra = None if carousel_style else _pick_extra_template(
        caption_lines, method_lines, jellyjob_lines, recap_lines
    )
    if extra == "template3":
        return _generate_template3(
            dest,
            set_index=set_index,
            augment=augment,
            quiet=quiet,
            output_dir=output_dir,
        )
    if extra == "template4":
        return _generate_template4(
            dest,
            set_index=set_index,
            avatar_pack=avatar_pack,
            photo_pack=photo_pack,
            augment=augment,
            quiet=quiet,
            output_dir=output_dir,
        )

    avatar_dir = _avatar_pack_dir(avatar_pack)
    avatars = _collect_images(avatar_dir)
    photos = _collect_photos(photo_pack, avatar_pack=avatar_pack)

    if not avatars:
        raise FileNotFoundError(f"Slide 1 needs an AVATAR — drop photos in: {avatar_dir}")
    if not photos:
        raise FileNotFoundError(
            f"Slides 2–4 need STYLE photos — drop files in: {PHOTOS_DIR / 'study_work'}"
        )

    if method_lines is None or jellyjob_lines is None or recap_lines is None:
        pair_style, paired_method, paired_jellyjob, paired_recap = _pick_paired_slides(
            carousel_style
        )
        if method_lines is None:
            method_lines = paired_method
        if jellyjob_lines is None:
            jellyjob_lines = paired_jellyjob
        if recap_lines is None:
            recap_lines = paired_recap
    else:
        pair_style = "custom"

    mapped = PAIR_STYLE_COLOR.get(pair_style)
    if mapped and mapped in TIKTOK_STYLE:
        color = mapped
    style = TIKTOK_STYLE.get(color, TIKTOK_STYLE[DEFAULT_COLOR])

    if caption_lines is None:
        hooks_by = _blocks_by_style("hooks")
        if hooks_by.get(pair_style):
            caption_lines = _pick_caption_set("hooks", style=pair_style)
        elif hooks_by.get("routine"):
            caption_lines = _pick_caption_set("hooks", style="routine")
        else:
            caption_lines = _pick_caption_set("hooks")

    # One effect for the whole carousel (random by default)
    aug = resolve_augment(augment)

    saved: list[Path] = []
    seed_base = random.randint(0, 1_000_000)
    prefix = "" if output_dir is not None else f"carousel_{set_index:02d}_"

    def _log(message: str) -> None:
        if not quiet:
            print(message)

    # --- Slide 1: AVATAR + hook only ---
    avatar_path = random.choice(avatars)
    bg1 = _load_cover_augmented(avatar_path, CANVAS_W, CANVAS_H, aug, seed_base)
    slide1 = render_slide_with_text(bg1, caption_lines, style)
    path1 = dest / f"{prefix}slide_00.png"
    slide1.save(path1, quality=95)
    saved.append(path1)
    _log(f"  Slide 1/4: {path1.name}  (HOOK + avatar [{avatar_pack}]: {avatar_path.name})")
    _log(f"  Caption style pair: {pair_style}  |  augment: {aug}")

    # --- Slides 2–4: STYLE photos + text ---
    pool = photos[:]
    random.shuffle(pool)
    while len(pool) < TYPE1_LIFESTYLE_COUNT:
        pool.extend(photos[:])
    picks = pool[:TYPE1_LIFESTYLE_COUNT]

    texts = (method_lines, jellyjob_lines, recap_lines)
    labels = ("GOOGLE", "JOBSHIFT", "RECAP")
    for i, (pick, text, label) in enumerate(zip(picks, texts, labels)):
        bg = _load_cover_augmented(pick, CANVAS_W, CANVAS_H, aug, seed_base + 1 + i)
        slide = render_slide_with_text(bg, text, style)
        path = dest / f"{prefix}slide_{i + 1:02d}.png"
        slide.save(path, quality=95)
        saved.append(path)
        _log(f"  Slide {i + 2}/4: {path.name}  ({label} + style [{pick.parent.name}]: {pick.name})")

    return CarouselBuild(
        slides=saved,
        pair_style=pair_style,
        augment=aug,
        color=color,
        avatar_name=avatar_path.name,
        photo_names=[pick.name for pick in picks],
        hook_preview=_caption_preview(caption_lines),
    )


def run_test(color: str = DEFAULT_COLOR) -> Path:
    """Black 9:16 previews: hook + paired routine & etape (Google + JobShift + recap)."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    style = TIKTOK_STYLE.get(color, TIKTOK_STYLE[DEFAULT_COLOR])
    bg = Image.new("RGB", (CANVAS_W, CANVAS_H), (0, 0, 0))

    random.seed(42)
    hook_img = render_slide_with_text(bg.copy(), _pick_caption_set("hooks"), style)
    out_hook = OUTPUT_DIR / "test_text_style_black.png"
    hook_img.save(out_hook, quality=95)
    print(f"Test hook saved: {out_hook}")

    for pair_style, seed in (("routine", 43), ("etape", 44), ("template2", 45)):
        random.seed(seed)
        theme = PAIR_STYLE_COLOR.get(pair_style, color)
        pair_style_colors = TIKTOK_STYLE.get(theme, style)
        for role in ("method", "jellyjob", "recap"):
            lines = _pick_caption_set(role, style=pair_style)
            img = render_slide_with_text(bg.copy(), lines, pair_style_colors)
            out = OUTPUT_DIR / f"test_{role}_{pair_style}.png"
            img.save(out, quality=95)
            print(f"Test {pair_style}/{role}: {out.name}")
        if pair_style == "template2":
            hook_lines = _pick_caption_set("hooks", style="template2")
            hook_img2 = render_slide_with_text(bg.copy(), hook_lines, pair_style_colors)
            out_hook2 = OUTPUT_DIR / "test_hooks_template2.png"
            hook_img2.save(out_hook2, quality=95)
            print(f"Test template2/hooks: {out_hook2.name}")

    return OUTPUT_DIR / "test_recap_routine.png"


def main():
    parser = argparse.ArgumentParser(description="Generate TikTok carousel images (9:16)")
    parser.add_argument("--test", action="store_true", help="Preview text style on black background")
    parser.add_argument(
        "--ab-test",
        action="store_true",
        help="Optional: compare all 4 augment methods side by side (inactive by default)",
    )
    parser.add_argument(
        "--augment",
        type=str,
        default=DEFAULT_AUGMENT,
        help=f"Photo augment: random (default), {', '.join(AUGMENT_METHODS)}, none",
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help="Source image path for --ab-test",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for --ab-test",
    )
    parser.add_argument("--color", type=str, default=DEFAULT_COLOR,
                        help=f"Color theme: {', '.join(TIKTOK_STYLE.keys())}")
    parser.add_argument("--sets", type=int, default=1, help="Number of carousels to generate")
    parser.add_argument(
        "--avatar",
        type=str,
        default=DEFAULT_AVATAR_PACK,
        help=f"Avatar pack under assets/avatars/ (default: {DEFAULT_AVATAR_PACK})",
    )
    parser.add_argument(
        "--photos",
        type=str,
        default=None,
        help="Photo pack under assets/photos/ (default: mix all packs)",
    )
    args = parser.parse_args()

    if args.color not in TIKTOK_STYLE:
        print(f"Unknown color '{args.color}'. Available: {', '.join(TIKTOK_STYLE.keys())}")
        return

    if args.test:
        run_test(args.color)
        return

    if args.ab_test:
        if not AB_TEST_ACTIVE:
            print("A/B test is temporarily inactive (normal gen uses --augment random).")
            print("Launching anyway since you passed --ab-test …")
        print("A/B photo augment test — compare crop / grade / grain / prop")
        src = Path(args.source) if args.source else None
        run_ab_test(source=src, seed=args.seed)
        return

    aug = args.augment.lower().strip()
    allowed = set(AUGMENT_METHODS) | {"none", "off", "random", "rand", "auto", ""}
    if aug not in allowed:
        print(f"Unknown --augment '{args.augment}'. Choose: random, {', '.join(AUGMENT_METHODS)}, none")
        return

    avatar_dir = _avatar_pack_dir(args.avatar)
    avatars = _collect_images(avatar_dir)
    photos = _collect_photos(args.photos, avatar_pack=args.avatar)
    packs = _list_avatar_packs()
    photo_packs = _list_photo_packs()

    print(f"Generating {args.sets} type-1 carousel(s) — 4 slides — {args.color}")
    print(f"  Slide 1 = AVATAR + hook  |  2 = Google  |  3 = JobShift  |  4 = Recap")
    print(f"  Avatar pack: {args.avatar} → {avatar_dir}")
    print(f"  Avatars: {len(avatars)} files")
    photo_label = args.photos or "all packs"
    print(f"  Style photos ({photo_label}): {len(photos)} files")
    print(f"  Augment mode: {aug or 'random'}  (1 effet au pif par carrousel)")
    if packs:
        print(f"  Available avatar packs: {', '.join(packs)}")
    if photo_packs:
        print(f"  Available photo packs: {', '.join(photo_packs)}")
    print()

    for s in range(args.sets):
        print(f"--- Carousel {s + 1}/{args.sets} ---")
        generate_type1_carousel(
            color=args.color,
            set_index=s,
            avatar_pack=args.avatar,
            photo_pack=args.photos,
            augment=aug or "random",
        )
        print()

    print(f"Done! Output in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
