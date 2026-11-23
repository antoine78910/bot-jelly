"""Unique variants of a 9:16 clip inside an X-style post frame, with uniqueness guards.

Needs ffmpeg (with the rubberband filter and libx264), ffprobe and fpcalc on PATH.
See README.md, section "Vidéo (bouton Vidéo)".
"""

from .media import MediaError, RenderCancelled, selfcheck
from .pipeline import GuardFailed, RenderResult, Signature, render_unique

__all__ = [
    "GuardFailed",
    "MediaError",
    "RenderCancelled",
    "RenderResult",
    "Signature",
    "render_unique",
    "selfcheck",
]
