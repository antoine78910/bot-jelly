"""X/Twitter-style chrome primitives: badge, icons, avatars, engagement, headers.

Ported from the bj-assets prototype (x_chrome.py). The Inter font is vendored in
./fonts (SIL OFL 1.1, see fonts/OFL.txt), so nothing depends on a host font path.
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

INTER_PATH = Path(__file__).resolve().parent / "fonts" / "Inter-VariableFont_opsz,wght.ttf"

# Official-adjacent X palette
X_BLUE = (29, 155, 240, 255)        # #1D9BF0 verified
X_TEXT = (15, 20, 25, 255)          # #0F1419
X_MUTED = (83, 100, 113, 255)       # #536471
X_WHITE = (255, 255, 255, 255)
X_CARD_LIGHT = (255, 255, 255, 255)
X_DARK_BG = (0, 0, 0, 255)
X_DARK_CARD = (22, 24, 28, 255)     # #16181C
X_DARK_TEXT = (231, 233, 234, 255)  # #E7E9EA
X_DARK_MUTED = (113, 118, 123, 255) # #71767B
X_DARK_BORDER = (47, 51, 54, 255)   # #2F3336
X_DIVIDER_LIGHT = (239, 243, 244, 255)  # #EFF3F4
X_DIVIDER_DARK = (47, 51, 54, 255)


@lru_cache(maxsize=64)
def inter(size: int, weight: int = 400) -> ImageFont.FreeTypeFont:
    if size < 20:
        opsz = 14
    elif size < 32:
        opsz = 18
    elif size < 48:
        opsz = 24
    else:
        opsz = 32
    font = ImageFont.truetype(str(INTER_PATH), size)
    font.set_variation_by_axes([float(opsz), float(weight)])
    return font


def _ssaa(draw_size: int, out_size: int, painter) -> Image.Image:
    """Paint on a supersampled canvas and lanczos-downscale."""
    im = Image.new("RGBA", (draw_size, draw_size), (0, 0, 0, 0))
    painter(im, draw_size)
    if draw_size != out_size:
        im = im.resize((out_size, out_size), Image.Resampling.LANCZOS)
    return im


def verified_badge(size: int) -> Image.Image:
    """X-style official check: scalloped blue seal + white checkmark."""
    scale = 4
    S = size * scale

    def paint(im: Image.Image, S: int) -> None:
        d = ImageDraw.Draw(im)
        cx = cy = S / 2
        # Scalloped seal: 8 rounded lobes + inner disk, classic Twitter shape.
        r = S * 0.36
        lobe_r = S * 0.22
        for i in range(8):
            ang = math.radians(i * 45.0)
            px = cx + r * 0.70 * math.cos(ang)
            py = cy + r * 0.70 * math.sin(ang)
            d.ellipse(
                (px - lobe_r, py - lobe_r, px + lobe_r, py + lobe_r),
                fill=X_BLUE,
            )
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=X_BLUE)
        # White check, slightly bold, optically centered.
        # Coordinates in unit space then scaled.
        stroke = max(2, int(S * 0.11))
        # Check path: lower-left to crotch to upper-right
        p1 = (cx - S * 0.20, cy + S * 0.02)
        p2 = (cx - S * 0.05, cy + S * 0.18)
        p3 = (cx + S * 0.22, cy - S * 0.16)
        # Draw as thick rounded lines by stamping circles along the segments
        def thick_line(a, b, w):
            ax, ay = a
            bx, by = b
            dist = math.hypot(bx - ax, by - ay)
            n = max(2, int(dist))
            r = w / 2
            for i in range(n + 1):
                t = i / n
                x = ax + (bx - ax) * t
                y = ay + (by - ay) * t
                d.ellipse((x - r, y - r, x + r, y + r), fill=X_WHITE)

        thick_line(p1, p2, stroke)
        thick_line(p2, p3, stroke)

    return _ssaa(S, size, paint)


def _stroke_line(d: ImageDraw.ImageDraw, a, b, width: float, color, cap=True):
    ax, ay = a
    bx, by = b
    dist = math.hypot(bx - ax, by - ay)
    n = max(2, int(dist) + 1)
    r = width / 2
    for i in range(n + 1):
        t = i / n
        x = ax + (bx - ax) * t
        y = ay + (by - ay) * t
        d.ellipse((x - r, y - r, x + r, y + r), fill=color)


def _stroke_polyline(d, pts, width, color):
    for a, b in zip(pts, pts[1:]):
        _stroke_line(d, a, b, width, color)


def icon_reply(size: int, color: tuple) -> Image.Image:
    """Filled speech-bubble (X reply)."""
    scale = 4
    S = size * scale

    def paint(im, S):
        d = ImageDraw.Draw(im)
        # Bubble body
        pad = S * 0.10
        d.rounded_rectangle(
            (pad, pad * 0.9, S - pad, S * 0.72),
            radius=S * 0.22,
            fill=color,
        )
        # Tail (bottom-left)
        tail = [
            (S * 0.22, S * 0.62),
            (S * 0.18, S * 0.90),
            (S * 0.48, S * 0.62),
        ]
        d.polygon(tail, fill=color)

    return _ssaa(S, size, paint)


def icon_repost(size: int, color: tuple) -> Image.Image:
    """Two cycling arrows (X repost/retweet)."""
    scale = 4
    S = size * scale

    def paint(im, S):
        d = ImageDraw.Draw(im)
        w = S * 0.105
        # Classic Twitter retweet: two open U-shaped arrows.
        # Upper: left vertical going up, then right, arrowhead pointing right.
        # Lower: right vertical going down, then left, arrowhead pointing left.
        xL, xR = S * 0.28, S * 0.72
        yT, yB = S * 0.30, S * 0.70
        mid_y_top = S * 0.50
        mid_y_bot = S * 0.50
        ah = S * 0.13  # arrowhead arm
        # Upper left vertical (from mid down-area up to top)
        _stroke_line(d, (xL, mid_y_top + S * 0.04), (xL, yT), w, color)
        _stroke_line(d, (xL, yT), (xR, yT), w, color)
        # arrowhead at top-right, pointing right
        _stroke_line(d, (xR - ah, yT - ah), (xR, yT), w, color)
        _stroke_line(d, (xR - ah, yT + ah), (xR, yT), w, color)
        # Lower right vertical + leftward bar
        _stroke_line(d, (xR, mid_y_bot - S * 0.04), (xR, yB), w, color)
        _stroke_line(d, (xR, yB), (xL, yB), w, color)
        _stroke_line(d, (xL + ah, yB - ah), (xL, yB), w, color)
        _stroke_line(d, (xL + ah, yB + ah), (xL, yB), w, color)

    return _ssaa(S, size, paint)


def icon_like(size: int, color: tuple) -> Image.Image:
    """Outline heart (X like)."""
    scale = 4
    S = size * scale

    def paint(im, S):
        d = ImageDraw.Draw(im)
        # Build a heart polygon and stroke it by drawing a filled heart
        # minus a smaller heart, then downscale.
        # Parametric heart, then offset.
        pts = []
        for i in range(80):
            t = i / 80 * 2 * math.pi
            # Classic heart parametric, flipped y, scaled and centered
            x = 16 * math.sin(t) ** 3
            y = (
                13 * math.cos(t)
                - 5 * math.cos(2 * t)
                - 2 * math.cos(3 * t)
                - math.cos(4 * t)
            )
            pts.append((x, -y))
        minx, maxx = min(p[0] for p in pts), max(p[0] for p in pts)
        miny, maxy = min(p[1] for p in pts), max(p[1] for p in pts)
        pad = S * 0.14
        sx = (S - 2 * pad) / (maxx - minx)
        sy = (S - 2 * pad) / (maxy - miny)
        s = min(sx, sy)
        cx = S / 2
        cy = S / 2 + S * 0.04
        mx = (minx + maxx) / 2
        my = (miny + maxy) / 2
        poly = [((x - mx) * s + cx, (y - my) * s + cy) for x, y in pts]
        # Stroke by drawing a thick polyline that closes
        w = S * 0.11
        _stroke_polyline(d, poly + [poly[0]], w, color)

    return _ssaa(S, size, paint)


def icon_views(size: int, color: tuple) -> Image.Image:
    """Three vertical bars (X views / impressions)."""
    scale = 4
    S = size * scale

    def paint(im, S):
        d = ImageDraw.Draw(im)
        # Three bars, left short, mid medium, right tall. Rounded caps.
        bar_w = S * 0.16
        gap = S * 0.10
        total = 3 * bar_w + 2 * gap
        x0 = (S - total) / 2
        heights = (0.42, 0.66, 0.92)
        bottom = S * 0.86
        for i, h in enumerate(heights):
            x = x0 + i * (bar_w + gap)
            top = bottom - S * h
            d.rounded_rectangle(
                (x, top, x + bar_w, bottom),
                radius=bar_w / 2,
                fill=color,
            )

    return _ssaa(S, size, paint)


def icon_bookmark(size: int, color: tuple) -> Image.Image:
    """Outline bookmark ribbon (X bookmark)."""
    scale = 4
    S = size * scale

    def paint(im, S):
        d = ImageDraw.Draw(im)
        w = S * 0.11
        left, right = S * 0.26, S * 0.74
        top, bot = S * 0.12, S * 0.90
        mid = S * 0.50
        notch = S * 0.62
        pts = [
            (left, top + S * 0.06),
            (left, bot),
            (mid, notch),
            (right, bot),
            (right, top + S * 0.06),
        ]
        # rounded top corners via thick stroke + top cap
        _stroke_polyline(d, pts, w, color)
        _stroke_line(d, (left, top + S * 0.06), (right, top + S * 0.06), w, color)

    return _ssaa(S, size, paint)


def draw_avatar(size: int, initials: str, color: tuple, icon: str = "letters") -> Image.Image:
    av = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(av)
    rgb = color[:3] if len(color) == 4 else color
    d.ellipse((0, 0, size - 1, size - 1), fill=rgb + (255,))
    if icon == "bolt":
        # Lightning bolt in a slightly darker/yellow-white
        cx, cy = size / 2, size / 2
        s = size
        bolt = [
            (cx + s * 0.08, cy - s * 0.32),
            (cx - s * 0.10, cy + s * 0.02),
            (cx + s * 0.02, cy + s * 0.02),
            (cx - s * 0.08, cy + s * 0.32),
            (cx + s * 0.12, cy - s * 0.04),
            (cx - s * 0.02, cy - s * 0.04),
        ]
        d.polygon(bolt, fill=(255, 224, 40, 255))
    elif icon == "eyes":
        # Two cartoon eyes, like the Insane Moments / Trending Reels avatars
        eye_r = size * 0.16
        pupil_r = size * 0.07
        cy = size * 0.50
        for cx in (size * 0.35, size * 0.65):
            d.ellipse(
                (cx - eye_r, cy - eye_r * 1.05, cx + eye_r, cy + eye_r * 1.05),
                fill=(255, 255, 255, 255),
            )
            d.ellipse(
                (cx - pupil_r, cy - pupil_r, cx + pupil_r, cy + pupil_r),
                fill=(20, 20, 20, 255),
            )
    else:
        font = inter(int(size * 0.38), 700)
        text = initials[:2]
        bbox = font.getbbox(text)
        x = (size - (bbox[2] - bbox[0])) / 2 - bbox[0]
        y = (size - (bbox[3] - bbox[1])) / 2 - bbox[1]
        # Pick a contrasting fill
        luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
        fill = (255, 255, 255, 255) if luma < 160 else (15, 20, 25, 255)
        d.text((x, y), text, font=font, fill=fill)
    return av


def format_x_count(n: int) -> str:
    """X-style count: 415 / 1,204 / 1.4K / 15.9K / 502.5K."""
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 10_000:
        if n % 100 == 0:
            k = n / 1000
            return f"{k:.1f}K" if k % 1 else f"{int(k)}K"
        return f"{n:,}"
    if n < 1_000_000:
        k = n / 1000.0
        if abs(k - round(k)) < 0.05:
            return f"{int(round(k))}K"
        return f"{k:.1f}K"
    m = n / 1_000_000.0
    if abs(m - round(m)) < 0.05:
        return f"{int(round(m))}M"
    return f"{m:.1f}M"


def format_time_12h(hour24: int, minute: int) -> str:
    period = "AM" if hour24 < 12 else "PM"
    hour = hour24 % 12
    if hour == 0:
        hour = 12
    return f"{hour}:{minute:02d} {period}"


def draw_engagement_bar(
    im: Image.Image,
    x: int,
    y: int,
    width: int,
    replies: int,
    reposts: int,
    likes: int,
    views: int,
    muted: tuple,
    icon_size: int = 28,
    include_bookmark: bool = True,
) -> int:
    """Draw the X engagement row. Returns the row height."""
    d = ImageDraw.Draw(im)
    count_font = inter(23, 500)
    items = [
        ("reply", format_x_count(replies), icon_reply(icon_size, muted)),
        ("repost", format_x_count(reposts), icon_repost(icon_size, muted)),
        ("like", format_x_count(likes), icon_like(icon_size, muted)),
        ("views", format_x_count(views), icon_views(icon_size, muted)),
    ]
    # Four counted columns, bookmark flush right.
    n = len(items)
    # Leave ~44px on the right for the bookmark if needed.
    bookmark_w = 44 if include_bookmark else 0
    col_w = (width - bookmark_w) / n
    row_h = max(icon_size, 28)
    for i, (_name, label, icon) in enumerate(items):
        cx = int(x + i * col_w)
        im.paste(icon, (cx, y), icon)
        tw = count_font.getlength(label)
        # Vertically center the count against the icon
        bbox = count_font.getbbox(label)
        text_y = y + (icon_size - (bbox[3] - bbox[1])) / 2 - bbox[1]
        d.text((cx + icon_size + 8, text_y), label, font=count_font, fill=muted)
    if include_bookmark:
        bm = icon_bookmark(icon_size, muted)
        im.paste(bm, (int(x + width - icon_size), y), bm)
    return icon_size + 2


def draw_timestamp_line(
    d: ImageDraw.ImageDraw,
    x: int,
    y: int,
    time_label: str,
    date_label: str,
    views: int,
    muted: tuple,
    strong: tuple,
) -> int:
    """'6:04 PM · Nov 22, 2026 · 502.5K Views'. Returns height."""
    regular = inter(23, 450)
    bold = inter(23, 700)
    views_str = format_x_count(views)
    left = f"{time_label} · {date_label} · "
    # Draw left muted, views count strong, ' Views' muted
    bbox_l = regular.getbbox(left)
    d.text((x, y - bbox_l[1]), left, font=regular, fill=muted)
    x2 = x + regular.getlength(left)
    bbox_v = bold.getbbox(views_str)
    d.text((x2, y - bbox_v[1]), views_str, font=bold, fill=strong)
    x3 = x2 + bold.getlength(views_str)
    rest = " Views"
    bbox_r = regular.getbbox(rest)
    d.text((x3, y - bbox_r[1]), rest, font=regular, fill=muted)
    return max(bbox_l[3] - bbox_l[1], bbox_v[3] - bbox_v[1]) + 2


def draw_user_header(
    im: Image.Image,
    x: int,
    y: int,
    account_name: str,
    handle: str,
    avatar: Image.Image,
    muted: tuple,
    text: tuple,
    verified: bool = True,
    relative: str | None = None,
    name_size: int = 32,
    handle_size: int = 24,
    gap: int = 14,
    show_handle: bool = True,
) -> int:
    """Avatar + name (+ badge) + handle (· relative). Returns header height."""
    av_s = avatar.size[0]
    im.paste(avatar, (x, y), avatar)
    d = ImageDraw.Draw(im)
    name_font = inter(name_size, 700)
    tx = x + av_s + gap
    nb = name_font.getbbox(account_name)
    name_h = nb[3] - nb[1]
    handle_h = 0
    hb_text = ""
    if show_handle and handle_size > 0:
        handle_font = inter(handle_size, 450)
        hb_text = f"@{handle}"
        if relative:
            hb_text = f"@{handle} · {relative}"
        hb = handle_font.getbbox(hb_text)
        handle_h = hb[3] - hb[1]
        stack_h = name_h + 4 + handle_h
    else:
        handle_font = None
        hb = None
        stack_h = name_h
    stack_y = y + (av_s - stack_h) // 2
    name_y = stack_y - nb[1]
    d.text((tx, name_y), account_name, font=name_font, fill=text)
    name_w = name_font.getlength(account_name)
    if verified:
        badge_s = max(26, int(name_size * 1.05))
        badge = verified_badge(badge_s)
        badge_x = int(tx + name_w + 6)
        badge_y = int(name_y + nb[1] + (name_h - badge_s) / 2)
        im.paste(badge, (badge_x, badge_y), badge)
    if handle_font is not None and hb is not None:
        d.text((tx, stack_y + name_h + 4 - hb[1]), hb_text, font=handle_font, fill=muted)
    return av_s


