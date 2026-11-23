"""ffmpeg / ffprobe / fpcalc wrappers: tool checks, bounded subprocesses, probing, frames.

Every subprocess has a timeout and can be cancelled through a threading.Event; on
either, the child is terminated, then killed. Nothing falls back to a weaker tool:
if ffmpeg, ffprobe, fpcalc or the rubberband filter is missing, selfcheck() says so
and the pipeline refuses to run.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

CANVAS_W, CANVAS_H = 1080, 1920
MAX_SOURCE_SECONDS = 180.0
MIN_SOURCE_SECONDS = 5.0
# Portrait close to 9:16 (0.5625). Small deviations are cropped, never stretched.
MIN_ASPECT, MAX_ASPECT = 0.50, 0.62
PROBE_TIMEOUT = 60
FRAME_TIMEOUT = 60
FINGERPRINT_TIMEOUT = 180


class MediaError(Exception):
    """Problem with a media file or tool. The message is French and safe to show staff."""


class RenderCancelled(Exception):
    """The job was cancelled (shutdown) while a subprocess was running."""


def _env_tool(env: str, name: str) -> str | None:
    explicit = os.getenv(env, "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    return shutil.which(name)


def ffmpeg_bin() -> str | None:
    return _env_tool("FFMPEG_PATH", "ffmpeg")


def ffprobe_bin() -> str | None:
    explicit = os.getenv("FFPROBE_PATH", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    ffmpeg = ffmpeg_bin()
    if ffmpeg:
        sibling = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe")


def fpcalc_bin() -> str | None:
    return _env_tool("FPCALC_PATH", "fpcalc")


def _require(path: str | None, name: str) -> str:
    if not path:
        raise MediaError(f"Outil `{name}` introuvable sur le serveur.")
    return path


def selfcheck() -> list[str]:
    """Problems that block the video feature (empty list = ready)."""
    problems: list[str] = []
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        problems.append("ffmpeg introuvable (paquet apt ffmpeg)")
    if not ffprobe_bin():
        problems.append("ffprobe introuvable (paquet apt ffmpeg)")
    if not fpcalc_bin():
        problems.append("fpcalc introuvable (paquet apt libchromaprint-tools)")
    if ffmpeg:
        try:
            filters = subprocess.run(
                [ffmpeg, "-hide_banner", "-filters"],
                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
            ).stdout
            encoders = subprocess.run(
                [ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
            ).stdout
        except (OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"ffmpeg ne répond pas ({exc.__class__.__name__})")
        else:
            if not any(line.split()[1:2] == ["rubberband"] for line in filters.splitlines()):
                problems.append("filtre ffmpeg rubberband absent (librubberband)")
            if " libx264 " not in encoders:
                problems.append("encodeur ffmpeg libx264 absent")
            if " aac " not in encoders:
                problems.append("encodeur ffmpeg aac absent")
    return problems


def run_cmd(
    cmd: list[str],
    *,
    timeout: float,
    cancel: threading.Event | None = None,
    stdout_path: Path | None = None,
) -> tuple[int, bytes, bytes]:
    """Run a command with a hard timeout and cooperative cancel. Returns (code, out, err)."""
    stdout_file = open(stdout_path, "wb") if stdout_path else None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file or subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + timeout
        while True:
            try:
                out, err = proc.communicate(timeout=0.5)
                return proc.returncode, out or b"", err or b""
            except subprocess.TimeoutExpired:
                cancelled = cancel is not None and cancel.is_set()
                if cancelled or time.monotonic() > deadline:
                    _stop(proc)
                    if cancelled:
                        raise RenderCancelled(Path(cmd[0]).name) from None
                    raise MediaError(
                        f"`{Path(cmd[0]).name}` a dépassé le délai de {int(timeout)} s."
                    ) from None
    finally:
        if stdout_file:
            stdout_file.close()


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

@dataclass
class SourceInfo:
    duration: float
    width: int  # display width (rotation applied)
    height: int
    has_audio: bool
    video_codec: str
    audio_codec: str


def probe(path: Path, *, cancel: threading.Event | None = None) -> dict:
    ffprobe = _require(ffprobe_bin(), "ffprobe")
    code, out, err = run_cmd(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=PROBE_TIMEOUT,
        cancel=cancel,
    )
    if code != 0:
        raise MediaError("Fichier vidéo illisible (ffprobe a échoué).")
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        raise MediaError("Fichier vidéo illisible (réponse ffprobe invalide).") from None
    return data if isinstance(data, dict) else {}


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                return int(float(side["rotation"]))
            except (TypeError, ValueError):
                return 0
    try:
        return int((stream.get("tags") or {}).get("rotate", 0))
    except (TypeError, ValueError):
        return 0


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def source_info(data: dict) -> SourceInfo:
    """Validate a probe for use as a source. Raises MediaError with a French reason."""
    streams = data.get("streams") or []
    video = next(
        (
            s for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MediaError("Le fichier ne contient pas de piste vidéo.")
    if audio is None:
        raise MediaError(
            "La vidéo n’a pas de piste audio. Le son est modifié pour chaque version, "
            "envoie une vidéo avec du son."
        )
    duration = _finite((data.get("format") or {}).get("duration")) or _finite(video.get("duration"))
    if duration is None:
        raise MediaError("Durée de la vidéo illisible.")
    if duration > MAX_SOURCE_SECONDS:
        raise MediaError(
            f"La vidéo dure {duration:.0f} s, le maximum est {MAX_SOURCE_SECONDS:.0f} s."
        )
    if duration < MIN_SOURCE_SECONDS:
        raise MediaError(
            f"La vidéo dure {duration:.1f} s, le minimum est {MIN_SOURCE_SECONDS:.0f} s."
        )
    try:
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    except (TypeError, ValueError):
        width = height = 0
    if width <= 0 or height <= 0:
        raise MediaError("Dimensions de la vidéo illisibles.")
    if abs(_rotation(video)) % 180 == 90:
        width, height = height, width
    aspect = width / height
    if not (MIN_ASPECT <= aspect <= MAX_ASPECT):
        raise MediaError(
            f"La vidéo fait {width}×{height}. Il faut une vidéo verticale 9:16 (ex. 1080×1920)."
        )
    return SourceInfo(
        duration=duration,
        width=width,
        height=height,
        has_audio=True,
        video_codec=str(video.get("codec_name") or ""),
        audio_codec=str(audio.get("codec_name") or ""),
    )


def output_problems(data: dict) -> list[str]:
    """Checks on a finished render: 1080x1920 H.264 60 fps + AAC 48 kHz, no metadata tags."""
    problems: list[str] = []
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        return ["no video stream"]
    if audio is None:
        problems.append("no audio stream")
    if (video.get("width"), video.get("height")) != (CANVAS_W, CANVAS_H):
        problems.append(f"size {video.get('width')}x{video.get('height')}")
    if video.get("codec_name") != "h264":
        problems.append(f"video codec {video.get('codec_name')}")
    if video.get("r_frame_rate") not in ("60/1",):
        problems.append(f"frame rate {video.get('r_frame_rate')}")
    if audio is not None:
        if audio.get("codec_name") != "aac":
            problems.append(f"audio codec {audio.get('codec_name')}")
        if str(audio.get("sample_rate")) != "48000":
            problems.append(f"sample rate {audio.get('sample_rate')}")
    tags = (data.get("format") or {}).get("tags") or {}
    leaked = sorted(k for k in tags if k.lower() not in {"major_brand", "minor_version", "compatible_brands", "encoder"})
    if leaked:
        problems.append(f"metadata tags left: {', '.join(leaked)}")
    if _finite((data.get("format") or {}).get("duration")) is None:
        problems.append("duration unreadable")
    return problems


# ---------------------------------------------------------------------------
# Frames and fingerprints
# ---------------------------------------------------------------------------

def grab_frame(path: Path, t: float, *, cancel: threading.Event | None = None) -> np.ndarray:
    """One RGB frame at t seconds, normalised to 1080x1920 (crop, no stretch)."""
    ffmpeg = _require(ffmpeg_bin(), "ffmpeg")
    code, out, _err = run_cmd(
        [
            ffmpeg, "-nostdin", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path),
            "-frames:v", "1",
            "-vf", f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=increase,crop={CANVAS_W}:{CANVAS_H}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        timeout=FRAME_TIMEOUT,
        cancel=cancel,
    )
    expected = CANVAS_W * CANVAS_H * 3
    if code != 0 or len(out) != expected:
        raise MediaError(f"Image à {t:.2f} s illisible.")
    return np.frombuffer(out, np.uint8).reshape(CANVAS_H, CANVAS_W, 3)


def fingerprint(path: Path, *, max_seconds: int = 400, cancel: threading.Event | None = None) -> np.ndarray:
    """Raw chromaprint (fpcalc) of the whole audio track as uint32."""
    fpcalc = _require(fpcalc_bin(), "fpcalc")
    code, out, _err = run_cmd(
        [fpcalc, "-raw", "-json", "-length", str(int(max_seconds)), str(path)],
        timeout=FINGERPRINT_TIMEOUT,
        cancel=cancel,
    )
    if code != 0:
        raise MediaError("Empreinte audio impossible (fpcalc a échoué).")
    try:
        values = json.loads(out.decode("utf-8", "replace"))["fingerprint"]
        array = np.asarray(values, dtype=np.int64)
    except (ValueError, KeyError, TypeError):
        raise MediaError("Empreinte audio illisible.") from None
    if array.ndim != 1:
        raise MediaError("Empreinte audio illisible.")
    # fpcalc prints raw values signed or unsigned depending on the version.
    return (array & 0xFFFFFFFF).astype(np.uint32)
