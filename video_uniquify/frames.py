"""X-style frames around the uncropped 9:16 clip: layouts, renderers, parameter sampling.

Ported from the bj-assets prototype (dedup.py). Changes: no host paths, the video
width floor is 55 % (was 54 %, quotes could shrink to 52 %), the pitch sign is
forced by the caller so siblings alternate, and the post date follows today.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .chrome import (
    X_DARK_BG,
    X_DARK_BORDER,
    X_DARK_CARD,
    X_DARK_MUTED,
    X_DARK_TEXT,
    X_MUTED,
    X_TEXT,
    X_WHITE,
    draw_avatar,
    draw_engagement_bar,
    draw_timestamp_line,
    draw_user_header,
    format_time_12h,
    inter,
)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

CANVAS_W, CANVAS_H = 1080, 1920

# Fictional X-style page accounts (always shown as verified).
PAGE_ACCOUNTS = [
    {"name": "Viral Moments", "handle": "viralmoments", "initials": "VM", "color": (230, 70, 55), "icon": "letters"},
    {"name": "Insane Moments", "handle": "insanemoments26", "initials": "IM", "color": (90, 205, 70), "icon": "eyes"},
    {"name": "Top Daily", "handle": "topdailyhq", "initials": "TD", "color": (110, 200, 75), "icon": "bolt"},
    {"name": "Viral Reels", "handle": "viralreels", "initials": "VR", "color": (50, 110, 230), "icon": "letters"},
    {"name": "Wild Moments", "handle": "wildmoments", "initials": "WM", "color": (230, 130, 40), "icon": "letters"},
    {"name": "Trending Reels", "handle": "trending_reelsx", "initials": "TR", "color": (70, 180, 220), "icon": "eyes"},
    {"name": "Viral Central", "handle": "viralcentral", "initials": "VC", "color": (90, 80, 230), "icon": "letters"},
    {"name": "Unreal Feed", "handle": "unrealfeed", "initials": "UF", "color": (230, 80, 140), "icon": "eyes"},
    {"name": "Clip Daily", "handle": "clipdaily", "initials": "CD", "color": (40, 180, 160), "icon": "bolt"},
    {"name": "Peak Clips", "handle": "peakclips", "initials": "PC", "color": (250, 160, 30), "icon": "letters"},
    {"name": "Daily Heat", "handle": "dailyheathq", "initials": "DH", "color": (230, 90, 50), "icon": "bolt"},
    {"name": "Moment Lab", "handle": "momentlab", "initials": "ML", "color": (120, 90, 220), "icon": "letters"},
]

# Regular (unverified) accounts used as the outer poster of a quote-tweet.
QUOTERS = [
    {"name": "tyler", "handle": "tyler", "initials": "T", "color": (230, 155, 80), "icon": "letters"},
    {"name": "brooke22", "handle": "brooke22", "initials": "B", "color": (155, 80, 220), "icon": "letters"},
    {"name": "alex.m", "handle": "alexm", "initials": "A", "color": (70, 160, 200), "icon": "letters"},
    {"name": "camille", "handle": "camille_r", "initials": "C", "color": (220, 100, 130), "icon": "letters"},
    {"name": "jordan", "handle": "jordan", "initials": "J", "color": (80, 190, 120), "icon": "letters"},
    {"name": "nina", "handle": "nina.codes", "initials": "N", "color": (200, 90, 180), "icon": "letters"},
    {"name": "leo", "handle": "leo_watch", "initials": "L", "color": (240, 170, 50), "icon": "letters"},
    {"name": "maya", "handle": "maya", "initials": "M", "color": (90, 130, 230), "icon": "letters"},
]

HOOKS = {
    "tweet_light": "Postuler sans lever le petit doigt, c'est l'IA qui s'en charge",
    "tweet_dark": "Je laisse un agent IA postuler à ma place",
    "quote_dark": "L'IA postule à ma place, franchement dingue",
}
QUOTE_REACTIONS = [
    "no way this is real",
    "ok ça change tout",
    "j'ai regardé ça 10 fois",
    "attends, c'est réel ?",
    "wait what",
]

TEMPLATES = ("tweet_light", "tweet_dark", "quote_dark")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def even(n: float) -> int:
    n = int(round(n))
    return n if n % 2 == 0 else n + 1


def text_size(font: ImageFont.FreeTypeFont, text: str) -> tuple[int, int]:
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def protect_punct(text: str) -> str:
    """Keep French punctuation attached to the previous word (no orphan '?')."""
    for punct in "?!;:":
        text = text.replace(f" {punct}", f"\u00A0{punct}")
    return text


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = protect_punct(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if font.getlength(trial) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            if font.getlength(word) <= max_width:
                current = word
            else:
                chunk = ""
                for ch in word:
                    if chunk and font.getlength(chunk + ch) > max_width:
                        lines.append(chunk)
                        chunk = ch
                    else:
                        chunk += ch
                current = chunk
    if current:
        lines.append(current)
    return lines or [""]


def fit_wrap(
    text: str,
    weight: int,
    max_width: int,
    max_lines: int,
    start_size: int,
    min_size: int = 28,
) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    best = None
    for size in range(start_size, min_size - 1, -2):
        font = inter(size, weight)
        lines = wrap_text(text, font, max_width)
        if len(lines) > max_lines:
            continue
        last = font.getlength(lines[-1]) if lines else 0
        orphan = len(lines) >= 2 and last < 0.42 * max_width
        if not orphan:
            return font, lines, size
        if best is None:
            best = (font, lines, size)
    if best:
        return best
    font = inter(min_size, weight)
    return font, wrap_text(text, font, max_width), min_size


def rounded_mask(w: int, h: int, radius: int) -> Image.Image:
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    return mask


def punch_rounded_hole(im: Image.Image, box: tuple[int, int, int, int], radius: int) -> None:
    """Make a rounded rectangle fully transparent (the video window)."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    region = im.crop((x0, y0, x1, y1))
    arr = np.array(region)
    mask = np.array(rounded_mask(w, h, radius))
    arr[..., 3] = np.minimum(arr[..., 3], (255 - mask).astype(np.uint8))
    im.paste(Image.fromarray(arr), (x0, y0))


# ---------------------------------------------------------------------------
# Layout / frame rendering
# ---------------------------------------------------------------------------

@dataclass
class FrameSpec:
    template: str
    hook: str
    width_frac: float
    video_w: int
    video_h: int
    video_x: int
    video_y: int
    radius: int
    bg_mode: str  # "blur" or "solid"
    bg_color: str  # ffmpeg color, e.g. "0x000000"
    time_label: str
    account: dict = field(default_factory=dict)
    quoter: dict = field(default_factory=dict)
    reaction: str = ""
    views: int = 0
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    clock_label: str = ""
    date_label: str = ""


def sample_engagement(rng: random.Random, today: date | None = None) -> dict:
    """Plausible X counts: likes 2-5% of views, reposts 5-15% of likes, replies lower."""
    bucket = rng.choice(("mid", "high", "viral"))
    if bucket == "mid":
        views = rng.randint(80_000, 250_000)
    elif bucket == "high":
        views = rng.randint(250_000, 700_000)
    else:
        views = rng.randint(700_000, 1_200_000)
    views = int(round(views / 100)) * 100
    likes = int(views * rng.uniform(0.02, 0.05))
    if likes >= 10_000:
        likes = int(round(likes / 100)) * 100
    elif likes >= 1000:
        likes = int(round(likes / 10)) * 10
    reposts = int(likes * rng.uniform(0.05, 0.15))
    if reposts >= 1000:
        reposts = int(round(reposts / 10)) * 10
    replies = int(reposts * rng.uniform(0.40, 0.90))
    replies = max(48, replies)
    hour24 = rng.choice([8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20])
    minute = rng.choice([0, 2, 4, 5, 7, 11, 14, 18, 22, 27, 31, 34, 41, 46, 52, 58])
    # Post date 1 to 3 days before today (the prototype hardcoded Nov 20-22, 2026).
    posted = (today or date.today()) - timedelta(days=rng.choice([1, 2, 3]))
    relative = rng.choice(["1h", "2h", "3h", "4h", "5h", "6h", "8h", "12h", "1d"])
    return {
        "views": views,
        "likes": likes,
        "reposts": reposts,
        "replies": replies,
        "clock_label": format_time_12h(hour24, minute),
        "date_label": f"{_MONTHS[posted.month - 1]} {posted.day}, {posted.year}",
        "relative": relative,
    }


def _caption_metrics(hook: str, max_width: int, start_size: int = 48, min_size: int = 34):
    font, lines, size = fit_wrap(hook, 450, max_width, 3, start_size, min_size)
    leading = int(size * 1.28)
    return font, lines, size, leading, leading * len(lines)


def layout_tweet_light(width_frac: float, rng: random.Random, chrome: dict) -> FrameSpec:
    hook = HOOKS["tweet_light"]
    vw = even(CANVAS_W * width_frac)
    vh = even(vw * CANVAS_H / CANVAS_W)
    pad = 40
    avatar = 80
    top_pad = 28
    gap_header = 20
    gap_caption = 20
    gap_video = 20
    gap_ts = 26
    eng_h = 34
    bottom_pad = 26
    # Caption uses the content width (card inner width, not just the video)
    # We don't know card_w yet; content width is video width (video is centered
    # but caption uses the inner card width = vw, left-aligned with padding).
    # For a wide card around the 9:16 video, the card is a bit wider than vw.
    side_inset = 28  # white margin around the 9:16 video inside the card
    inner_w = vw  # caption width equals video width (video is full content width)
    _font, _lines, _size, _lead, caption_h = _caption_metrics(hook, inner_w, 48, 34)
    header_h = avatar
    ts_h = 26
    card_w = vw + pad * 2
    card_h = (
        top_pad
        + header_h
        + gap_header
        + caption_h
        + gap_caption
        + vh
        + gap_video
        + ts_h
        + gap_ts
        + eng_h
        + bottom_pad
    )
    margin_top = 160
    margin_bot = 140
    slack_y = CANVAS_H - card_h - margin_top - margin_bot
    if slack_y < 0:
        margin_top = 80
        margin_bot = 80
        slack_y = max(0, CANVAS_H - card_h - margin_top - margin_bot)
    card_y = margin_top + slack_y // 3 + even(rng.randint(-12, 12))
    card_y = max(margin_top, min(card_y, CANVAS_H - card_h - margin_bot))
    slack_x = CANVAS_W - card_w
    card_x = even(max(24, slack_x // 2 + rng.randint(-16, 16)))
    card_x = max(24, min(card_x, CANVAS_W - card_w - 24))
    video_x = card_x + pad
    video_y = card_y + top_pad + header_h + gap_header + caption_h + gap_caption
    return FrameSpec(
        template="tweet_light",
        hook=hook,
        width_frac=width_frac,
        video_w=vw,
        video_h=vh,
        video_x=even(video_x),
        video_y=even(video_y),
        radius=22,
        bg_mode="blur",
        bg_color="0x000000",
        time_label=chrome["relative"],
        account=chrome["account"],
        views=chrome["views"],
        likes=chrome["likes"],
        reposts=chrome["reposts"],
        replies=chrome["replies"],
        clock_label=chrome["clock_label"],
        date_label=chrome["date_label"],
    )


def _tweet_card_metrics(spec: FrameSpec, pad: int = 40) -> dict:
    avatar = 80
    top_pad = 28
    gap_header = 20
    gap_caption = 20
    gap_video = 20
    gap_ts = 26
    eng_h = 34
    bottom_pad = 26
    hook_font, cap_lines, cap_size, cap_leading, caption_h = _caption_metrics(
        spec.hook, spec.video_w, 48, 34
    )
    header_h = avatar
    ts_h = 26
    card_w = spec.video_w + pad * 2
    card_x = spec.video_x - pad
    card_y = spec.video_y - (top_pad + header_h + gap_header + caption_h + gap_caption)
    card_h = (
        top_pad
        + header_h
        + gap_header
        + caption_h
        + gap_caption
        + spec.video_h
        + gap_video
        + ts_h
        + gap_ts
        + eng_h
        + bottom_pad
    )
    return {
        "pad": pad,
        "avatar": avatar,
        "top_pad": top_pad,
        "gap_header": gap_header,
        "gap_caption": gap_caption,
        "gap_video": gap_video,
        "gap_ts": gap_ts,
        "eng_h": eng_h,
        "bottom_pad": bottom_pad,
        "hook_font": hook_font,
        "cap_lines": cap_lines,
        "cap_size": cap_size,
        "cap_leading": cap_leading,
        "caption_h": caption_h,
        "header_h": header_h,
        "ts_h": ts_h,
        "card_w": card_w,
        "card_h": card_h,
        "card_x": card_x,
        "card_y": card_y,
        "card_r": 36,
    }


def render_tweet_light(spec: FrameSpec) -> Image.Image:
    im = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    m = _tweet_card_metrics(spec)
    card_x, card_y, card_w, card_h, card_r = m["card_x"], m["card_y"], m["card_w"], m["card_h"], m["card_r"]
    pad = m["pad"]

    shadow = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle(
        (card_x + 4, card_y + 10, card_x + card_w + 4, card_y + card_h + 10),
        radius=card_r,
        fill=(0, 0, 0, 150),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    im = Image.alpha_composite(im, shadow)

    d = ImageDraw.Draw(im)
    d.rounded_rectangle(
        (card_x, card_y, card_x + card_w, card_y + card_h),
        radius=card_r,
        fill=X_WHITE,
    )

    av_s = m["avatar"]
    av = draw_avatar(av_s, spec.account["initials"], spec.account["color"], spec.account["icon"])
    av_x = card_x + pad
    av_y = card_y + m["top_pad"]
    draw_user_header(
        im,
        av_x,
        av_y,
        spec.account["name"],
        spec.account["handle"],
        av,
        X_MUTED,
        X_TEXT,
        verified=True,
        relative=None,
        name_size=32,
        handle_size=23,
        gap=16,
    )
    d = ImageDraw.Draw(im)

    cap_y = av_y + m["header_h"] + m["gap_header"]
    for i, line in enumerate(m["cap_lines"]):
        bbox = m["hook_font"].getbbox(line)
        d.text(
            (card_x + pad, cap_y + i * m["cap_leading"] - bbox[1]),
            line,
            font=m["hook_font"],
            fill=X_TEXT,
        )

    punch_rounded_hole(
        im,
        (spec.video_x, spec.video_y, spec.video_x + spec.video_w, spec.video_y + spec.video_h),
        spec.radius,
    )
    d = ImageDraw.Draw(im)

    ts_y = spec.video_y + spec.video_h + m["gap_video"]
    draw_timestamp_line(
        d, card_x + pad, ts_y, spec.clock_label, spec.date_label, spec.views, X_MUTED, X_TEXT
    )
    eng_y = ts_y + m["ts_h"] + m["gap_ts"]
    draw_engagement_bar(
        im,
        card_x + pad,
        eng_y,
        card_w - pad * 2,
        spec.replies,
        spec.reposts,
        spec.likes,
        spec.views,
        X_MUTED,
        icon_size=30,
        include_bookmark=True,
    )
    return im


def layout_tweet_dark(width_frac: float, rng: random.Random, chrome: dict) -> FrameSpec:
    hook = HOOKS["tweet_dark"]
    vw = even(CANVAS_W * width_frac)
    vh = even(vw * CANVAS_H / CANVAS_W)
    pad = 40
    avatar = 80
    top_pad = 28
    gap_header = 20
    gap_caption = 20
    gap_video = 20
    gap_ts = 26
    eng_h = 34
    bottom_pad = 26
    _font, _lines, _size, _lead, caption_h = _caption_metrics(hook, vw, 48, 34)
    card_w = vw + pad * 2
    card_h = (
        top_pad + avatar + gap_header + caption_h + gap_caption + vh
        + gap_video + 26 + gap_ts + eng_h + bottom_pad
    )
    margin_top = 140
    margin_bot = 120
    slack_y = CANVAS_H - card_h - margin_top - margin_bot
    if slack_y < 0:
        margin_top = 70
        margin_bot = 70
        slack_y = max(0, CANVAS_H - card_h - margin_top - margin_bot)
    card_y = margin_top + slack_y // 3 + even(rng.randint(-12, 12))
    card_y = max(margin_top, min(card_y, CANVAS_H - card_h - margin_bot))
    slack_x = CANVAS_W - card_w
    card_x = even(max(24, slack_x // 2 + rng.randint(-16, 16)))
    card_x = max(24, min(card_x, CANVAS_W - card_w - 24))
    return FrameSpec(
        template="tweet_dark",
        hook=hook,
        width_frac=width_frac,
        video_w=vw,
        video_h=vh,
        video_x=even(card_x + pad),
        video_y=even(card_y + top_pad + avatar + gap_header + caption_h + gap_caption),
        radius=22,
        bg_mode="solid",
        bg_color="0x000000",
        time_label=chrome["relative"],
        account=chrome["account"],
        views=chrome["views"],
        likes=chrome["likes"],
        reposts=chrome["reposts"],
        replies=chrome["replies"],
        clock_label=chrome["clock_label"],
        date_label=chrome["date_label"],
    )


def render_tweet_dark(spec: FrameSpec) -> Image.Image:
    im = Image.new("RGBA", (CANVAS_W, CANVAS_H), X_DARK_BG)
    m = _tweet_card_metrics(spec)
    card_x, card_y, card_w, card_h, card_r = m["card_x"], m["card_y"], m["card_w"], m["card_h"], m["card_r"]
    pad = m["pad"]
    d = ImageDraw.Draw(im)
    d.rounded_rectangle(
        (card_x, card_y, card_x + card_w, card_y + card_h),
        radius=card_r,
        fill=X_DARK_CARD,
        outline=X_DARK_BORDER,
        width=2,
    )
    av_s = m["avatar"]
    av = draw_avatar(av_s, spec.account["initials"], spec.account["color"], spec.account["icon"])
    av_x = card_x + pad
    av_y = card_y + m["top_pad"]
    draw_user_header(
        im,
        av_x,
        av_y,
        spec.account["name"],
        spec.account["handle"],
        av,
        X_DARK_MUTED,
        X_DARK_TEXT,
        verified=True,
        relative=None,
        name_size=32,
        handle_size=23,
        gap=16,
    )
    d = ImageDraw.Draw(im)
    cap_y = av_y + m["header_h"] + m["gap_header"]
    for i, line in enumerate(m["cap_lines"]):
        bbox = m["hook_font"].getbbox(line)
        d.text(
            (card_x + pad, cap_y + i * m["cap_leading"] - bbox[1]),
            line,
            font=m["hook_font"],
            fill=X_DARK_TEXT,
        )
    punch_rounded_hole(
        im,
        (spec.video_x, spec.video_y, spec.video_x + spec.video_w, spec.video_y + spec.video_h),
        spec.radius,
    )
    d = ImageDraw.Draw(im)
    ts_y = spec.video_y + spec.video_h + m["gap_video"]
    draw_timestamp_line(
        d, card_x + pad, ts_y, spec.clock_label, spec.date_label, spec.views, X_DARK_MUTED, X_DARK_TEXT
    )
    eng_y = ts_y + m["ts_h"] + m["gap_ts"]
    draw_engagement_bar(
        im,
        card_x + pad,
        eng_y,
        card_w - pad * 2,
        spec.replies,
        spec.reposts,
        spec.likes,
        spec.views,
        X_DARK_MUTED,
        icon_size=30,
        include_bookmark=True,
    )
    return im


def _quote_metrics(vw: int, hook: str, reaction: str) -> dict:
    avatar = 72
    inner_avatar = 40
    quote_pad = 22
    hook_font, hook_lines, hook_size = fit_wrap(hook, 450, vw, 3, 36, 28)
    hook_leading = int(hook_size * 1.28)
    hook_h = hook_leading * len(hook_lines)
    reaction_font = inter(42, 450)
    rb = reaction_font.getbbox(reaction)
    reaction_h = rb[3] - rb[1]
    return {
        "avatar": avatar,
        "inner_avatar": inner_avatar,
        "quote_pad": quote_pad,
        "hook_font": hook_font,
        "hook_lines": hook_lines,
        "hook_size": hook_size,
        "hook_leading": hook_leading,
        "hook_h": hook_h,
        "reaction_font": reaction_font,
        "reaction_h": reaction_h,
        "gap_header_reaction": 16,
        "gap_reaction_box": 18,
        "gap_hook_video": 16,
        "inner_header_h": inner_avatar,
        "gap_inner_hook": 14,
        "eng_h": 34,
        "gap_box_eng": 22,
        "side": 56,
    }


def layout_quote(width_frac: float, rng: random.Random, chrome: dict) -> FrameSpec:
    hook = HOOKS["quote_dark"]
    reaction = chrome.get("reaction") or QUOTE_REACTIONS[0]
    vw = even(CANVAS_W * width_frac)
    vh = even(vw * CANVAS_H / CANVAS_W)
    m = _quote_metrics(vw, hook, reaction)
    # Inner header (quoted account) sits above the hook, above the video.
    chrome_h = (
        m["avatar"]
        + m["gap_header_reaction"]
        + m["reaction_h"]
        + m["gap_reaction_box"]
        + m["quote_pad"]
        + m["inner_header_h"]
        + m["gap_inner_hook"]
        + m["hook_h"]
        + m["gap_hook_video"]
        + vh
        + m["quote_pad"]
        + m["gap_box_eng"]
        + m["eng_h"]
    )
    top = 100 + rng.randint(-8, 16)
    bottom_margin = 80
    overflow = top + chrome_h - (CANVAS_H - bottom_margin)
    if overflow > 0:
        top = max(48, int(top - overflow))
        overflow = top + chrome_h - (CANVAS_H - bottom_margin)
    if overflow > 0:
        scale = (vh - overflow) / vh
        vw = even(max(CANVAS_W * WIDTH_MIN, vw * scale))
        vh = even(vw * CANVAS_H / CANVAS_W)
        m = _quote_metrics(vw, hook, reaction)
    video_y = even(
        top
        + m["avatar"]
        + m["gap_header_reaction"]
        + m["reaction_h"]
        + m["gap_reaction_box"]
        + m["quote_pad"]
        + m["inner_header_h"]
        + m["gap_inner_hook"]
        + m["hook_h"]
        + m["gap_hook_video"]
    )
    # Center the quote box; video is inset by quote_pad.
    box_w = vw + m["quote_pad"] * 2
    box_x = even((CANVAS_W - box_w) // 2)
    box_x = max(40, min(box_x, CANVAS_W - box_w - 40))
    video_x = box_x + m["quote_pad"]
    return FrameSpec(
        template="quote_dark",
        hook=hook,
        width_frac=width_frac,
        video_w=vw,
        video_h=vh,
        video_x=even(video_x),
        video_y=video_y,
        radius=20,
        bg_mode="solid",
        bg_color="0x000000",
        time_label=chrome["relative"],
        account=chrome["account"],
        quoter=chrome.get("quoter") or QUOTERS[0],
        reaction=reaction,
        views=chrome["views"],
        likes=chrome["likes"],
        reposts=chrome["reposts"],
        replies=chrome["replies"],
        clock_label=chrome["clock_label"],
        date_label=chrome["date_label"],
    )


def render_quote(spec: FrameSpec) -> Image.Image:
    im = Image.new("RGBA", (CANVAS_W, CANVAS_H), X_DARK_BG)
    reaction = spec.reaction or QUOTE_REACTIONS[0]
    m = _quote_metrics(spec.video_w, spec.hook, reaction)
    quote_pad = m["quote_pad"]
    box_x0 = spec.video_x - quote_pad
    box_x1 = spec.video_x + spec.video_w + quote_pad
    # Inner header + hook sit above the video inside the box.
    inner_top = (
        spec.video_y
        - m["gap_hook_video"]
        - m["hook_h"]
        - m["gap_inner_hook"]
        - m["inner_header_h"]
        - quote_pad
    )
    box_y0 = inner_top
    box_y1 = spec.video_y + spec.video_h + quote_pad
    header_y = (
        box_y0
        - m["gap_reaction_box"]
        - m["reaction_h"]
        - m["gap_header_reaction"]
        - m["avatar"]
    )

    quoter = spec.quoter or QUOTERS[0]
    av = draw_avatar(m["avatar"], quoter["initials"], quoter["color"], quoter["icon"])
    draw_user_header(
        im,
        box_x0,
        header_y,
        quoter["name"],
        quoter["handle"],
        av,
        X_DARK_MUTED,
        X_DARK_TEXT,
        verified=False,
        relative=spec.time_label,
        name_size=30,
        handle_size=23,
        gap=14,
    )
    d = ImageDraw.Draw(im)
    rb = m["reaction_font"].getbbox(reaction)
    react_y = header_y + m["avatar"] + m["gap_header_reaction"]
    d.text((box_x0, react_y - rb[1]), reaction, font=m["reaction_font"], fill=X_DARK_TEXT)

    d.rounded_rectangle(
        (box_x0, box_y0, box_x1, box_y1),
        radius=28,
        fill=X_DARK_BG,
        outline=X_DARK_BORDER,
        width=2,
    )

    # Quoted account (verified) inside the card.
    inner_av = draw_avatar(
        m["inner_avatar"], spec.account["initials"], spec.account["color"], spec.account["icon"]
    )
    inner_y = box_y0 + quote_pad
    draw_user_header(
        im,
        spec.video_x,
        inner_y,
        spec.account["name"],
        spec.account["handle"],
        inner_av,
        X_DARK_MUTED,
        X_DARK_TEXT,
        verified=True,
        relative=None,
        name_size=26,
        handle_size=23,
        gap=12,
        show_handle=False,
    )

    d = ImageDraw.Draw(im)
    hook_y = inner_y + m["inner_header_h"] + m["gap_inner_hook"]
    for i, line in enumerate(m["hook_lines"]):
        bbox = m["hook_font"].getbbox(line)
        d.text(
            (spec.video_x, hook_y + i * m["hook_leading"] - bbox[1]),
            line,
            font=m["hook_font"],
            fill=X_DARK_TEXT,
        )

    punch_rounded_hole(
        im,
        (spec.video_x, spec.video_y, spec.video_x + spec.video_w, spec.video_y + spec.video_h),
        spec.radius,
    )

    eng_y = box_y1 + m["gap_box_eng"]
    draw_engagement_bar(
        im,
        box_x0,
        eng_y,
        box_x1 - box_x0,
        spec.replies,
        spec.reposts,
        spec.likes,
        spec.views,
        X_DARK_MUTED,
        icon_size=30,
        include_bookmark=True,
    )
    return im


RENDERERS = {
    "tweet_light": (layout_tweet_light, render_tweet_light),
    "tweet_dark": (layout_tweet_dark, render_tweet_dark),
    "quote_dark": (layout_quote, render_quote),
}


# ---------------------------------------------------------------------------
# Parameter sampling
# ---------------------------------------------------------------------------

@dataclass
class VariantParams:
    name: str
    seed: int
    attempt: int
    template: str
    hook: str
    speed: float
    semitones: float
    pitch_ratio: float
    width_frac: float
    trim: float
    brightness: float
    saturation: float
    time_label: str
    video_w: int
    video_h: int
    video_x: int
    video_y: int
    radius: int
    bg_mode: str
    bg_color: str
    pitch_sign: int = 1
    account_name: str = ""
    account_handle: str = ""
    quoter_name: str = ""
    quoter_handle: str = ""
    reaction: str = ""
    views: int = 0
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    clock_label: str = ""
    date_label: str = ""
    account: dict = field(default_factory=dict)
    quoter: dict = field(default_factory=dict)


# Video window width as a fraction of 1080. Hard bounds 55-78 %; the prototype
# allowed 0.54 for tweets and shrank quotes down to 0.52, both below the floor.
WIDTH_MIN, WIDTH_MAX = 0.55, 0.78
WIDTH_RANGES = {
    "tweet_light": (0.55, 0.63),
    "tweet_dark": (0.55, 0.63),
    "quote_dark": (0.68, 0.74),
}
SPEED_RANGE = (1.04, 1.15)
SEMITONE_RANGE = (1.0, 2.0)


def sample_params(
    name: str,
    seed: int,
    template: str,
    attempt: int,
    *,
    pitch_sign: int,
    account: dict | None = None,
    quoter: dict | None = None,
    reaction: str | None = None,
    today: date | None = None,
) -> VariantParams:
    """Deterministic for a given seed. pitch_sign is forced by the caller (siblings alternate)."""
    if pitch_sign not in (-1, 1):
        raise ValueError("pitch_sign must be -1 or +1")
    rng = random.Random(seed)
    # Pitch floor 1.0 st, never 0, as learned from the Abondy comparison.
    speed = round(rng.uniform(*SPEED_RANGE), 4)
    rng.choice((-1, 1))  # keep the prototype's draw order so the other params stay comparable
    semitones = round(pitch_sign * rng.uniform(*SEMITONE_RANGE), 3)
    pitch_ratio = round(2 ** (semitones / 12.0), 5)
    lo, hi = WIDTH_RANGES[template]
    width_frac = round(rng.uniform(lo, hi), 4)
    trim = round(rng.uniform(0.0, 0.20), 3)
    brightness = round(rng.uniform(-0.04, 0.02), 4)
    saturation = round(rng.uniform(0.92, 1.12), 4)
    eng = sample_engagement(rng, today)
    account = account or rng.choice(PAGE_ACCOUNTS)
    quoter = quoter or rng.choice(QUOTERS)
    reaction = reaction or rng.choice(QUOTE_REACTIONS)
    chrome = {
        "account": account,
        "quoter": quoter,
        "reaction": reaction,
        **eng,
    }
    layout_fn, _ = RENDERERS[template]
    spec = layout_fn(width_frac, rng, chrome)
    final_frac = spec.video_w / CANVAS_W
    if not (WIDTH_MIN - 0.002 <= final_frac <= WIDTH_MAX + 0.002):
        raise ValueError(f"video width {final_frac:.4f} outside {WIDTH_MIN}-{WIDTH_MAX}")
    return VariantParams(
        name=name,
        seed=seed,
        attempt=attempt,
        template=template,
        hook=spec.hook,
        speed=speed,
        semitones=semitones,
        pitch_ratio=pitch_ratio,
        width_frac=final_frac,
        trim=trim,
        brightness=brightness,
        saturation=saturation,
        time_label=spec.time_label,
        video_w=spec.video_w,
        video_h=spec.video_h,
        video_x=spec.video_x,
        video_y=spec.video_y,
        radius=spec.radius,
        bg_mode=spec.bg_mode,
        bg_color=spec.bg_color,
        pitch_sign=pitch_sign,
        account_name=account["name"],
        account_handle=account["handle"],
        quoter_name=quoter["name"] if template == "quote_dark" else "",
        quoter_handle=quoter["handle"] if template == "quote_dark" else "",
        reaction=reaction if template == "quote_dark" else "",
        views=spec.views,
        likes=spec.likes,
        reposts=spec.reposts,
        replies=spec.replies,
        clock_label=spec.clock_label,
        date_label=spec.date_label,
        account=account,
        quoter=quoter if template == "quote_dark" else {},
    )


def render_frame(params: VariantParams, path: Path) -> Path:
    _, renderer = RENDERERS[params.template]
    spec = FrameSpec(
        template=params.template,
        hook=params.hook,
        width_frac=params.width_frac,
        video_w=params.video_w,
        video_h=params.video_h,
        video_x=params.video_x,
        video_y=params.video_y,
        radius=params.radius,
        bg_mode=params.bg_mode,
        bg_color=params.bg_color,
        time_label=params.time_label,
        account=params.account,
        quoter=params.quoter,
        reaction=params.reaction,
        views=params.views,
        likes=params.likes,
        reposts=params.reposts,
        replies=params.replies,
        clock_label=params.clock_label,
        date_label=params.date_label,
    )
    im = renderer(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG")
    return path


