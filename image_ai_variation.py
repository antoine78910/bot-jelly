"""
Optional AI-powered image variation for carousel slides, via fal.ai.

This sits alongside the existing deterministic Pillow augments in
carousel/generate_carousel.py (crop/grade/grain/prop). Those are free,
instant, and already vary crop/zoom/color/grain per export. This module
adds a stronger "remix" option that lightly regenerates the image with a
diffusion model (Ideogram V3 Remix) — at a low strength it keeps the same
composition but can shift small details/objects and textures, on top of a
fresh color grade, which the pure-crop/color methods can't do on their own.

Opt-in only:
  - Requires FAL_KEY in .env (https://fal.ai/dashboard/keys)
  - Requires CAROUSEL_AI_VARIATION=true in .env to enter the random augment
    pool (see carousel/generate_carousel.py: _ai_augment_enabled()).
Without FAL_KEY configured, callers should fall back to the local augments —
this module never silently fails a whole carousel render.
"""

from __future__ import annotations

import base64
import io
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

FAL_REMIX_URL = "https://fal.run/fal-ai/ideogram/v3/remix"

DEFAULT_PROMPTS = [
    "subtle natural color grade, keep exact composition and framing, "
    "photorealistic, minor lighting variation",
    "slightly different ambient lighting and color temperature, same scene "
    "and layout, photorealistic, candid photo",
    "gentle recomposition of background clutter, same subject and framing, "
    "photorealistic candid photo, natural color grade",
]


class ImageVariationError(Exception):
    pass


def fal_configured() -> bool:
    return bool(os.getenv("FAL_KEY", "").strip())


def _image_data_uri(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    mime = "image/png" if suffix == "png" else "image/jpeg"
    data = path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def remix_image_via_fal(
    path: Path,
    *,
    prompt: str | None = None,
    strength: float = 0.24,
    timeout: float = 90.0,
) -> bytes:
    """
    Send an image to fal.ai's Ideogram V3 Remix and return the resulting
    image bytes. Raises ImageVariationError on any failure — callers should
    catch this and fall back to a local augment.
    """
    key = os.getenv("FAL_KEY", "").strip()
    if not key:
        raise ImageVariationError("FAL_KEY is not configured.")

    import random

    chosen_prompt = prompt or random.choice(DEFAULT_PROMPTS)
    body = json.dumps(
        {
            "image_url": _image_data_uri(path),
            "prompt": chosen_prompt,
            "strength": max(0.05, min(0.6, strength)),
            "expand_prompt": False,
            "rendering_speed": "TURBO",
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        FAL_REMIX_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Key {key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise ImageVariationError(f"fal.ai HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ImageVariationError(f"fal.ai unreachable: {exc}") from exc
    except (json.JSONDecodeError, TimeoutError) as exc:
        raise ImageVariationError(f"fal.ai bad response: {exc}") from exc

    images = payload.get("images") or []
    if not images or not images[0].get("url"):
        raise ImageVariationError("fal.ai returned no image.")

    image_url = images[0]["url"]
    try:
        with urllib.request.urlopen(image_url, timeout=timeout) as response:
            return response.read()
    except urllib.error.URLError as exc:
        raise ImageVariationError(f"Could not download fal.ai result: {exc}") from exc


def remix_image_to_pil(path: Path, *, prompt: str | None = None, strength: float = 0.24):
    """Convenience wrapper returning a PIL Image (import kept local/optional)."""
    from PIL import Image

    result_bytes = remix_image_via_fal(path, prompt=prompt, strength=strength)
    return Image.open(io.BytesIO(result_bytes)).convert("RGB")
