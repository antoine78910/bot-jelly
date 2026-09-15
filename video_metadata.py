"""
MP4/MOV metadata stripping + subtle re-encode variation.

Goal: remove identity-revealing container metadata (device model, GPS,
original timestamps, editing-software fingerprints) and apply small,
imperceptible visual/audio variations so two exports of the same source
clip are not byte-identical and don't share an obvious re-encode
signature. This is standard "clean before you share" video hygiene —
the same class of thing privacy tools like ExifTool/Handbrake/MetaClean
do for video files. It does not defeat platform-side perceptual/content
matching (TikTok/Meta re-encode everything server-side anyway); it just
means the *file you upload* carries no extra identifying baggage and
looks like a fresh, independent export rather than a duplicate.

Fields known to carry identity in MP4/MOV containers (see docstring at
IDENTITY_FIELDS_INFO below for the full research summary).
"""

from __future__ import annotations

import json
import random
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from clip_assembler import (
    ClipAssemblyError,
    ffmpeg_available,
    resolve_ffmpeg_bin,
    resolve_ffprobe_bin,
)

# ---------------------------------------------------------------------------
# Research summary — what can identify a video / its source device
# ---------------------------------------------------------------------------
IDENTITY_FIELDS_INFO = """\
**Container metadata (removed):**
• `creation_time`, `com.apple.quicktime.creationdate` — original recording timestamp
• `com.apple.quicktime.location.ISO6709`, `location`, `location-eng` — GPS
• `com.apple.quicktime.make` / `.model` / `.software` — iPhone/camera model + OS build
• `com.android.manufacturer` / `.model` / `.version` / `.capture.fps` — Android device
• `com.apple.quicktime.content.identifier` — Apple's per-clip content ID (links Live Photos/exports)
• `comment`, `title`, `description`, `copyright`, `artist` — free-text tags some editors add
• `encoder`, `handler_name`, "Writing library" (Lavf/Lavc build string) — reveals the tool/version used to export
• Chapters, disposition flags, cover art — rarely used but scanned by some tools

**Re-encode signature (randomized, not just copied):**
Even with metadata stripped, an exact re-encode of the *same* source with the
*same* encoder settings (crf/preset/bitrate ladder/keyframe interval) produces a
near-identical byte pattern across exports — that pattern itself is a weak
fingerprint. We randomize crf/preset per export and force `bitexact` muxing to
drop the ffmpeg build string.

**What platforms already handle:**
TikTok/Meta re-encode every public upload server-side, which strips most of the
above automatically. The main practical benefit of doing it client-side is (1)
privacy — nothing leaks before their re-encode runs, and (2) two exports of the
same base clip no longer share an identical container/byte signature or an
identical crop/color/timing, so they don't look like a copy-paste of each other.
"""

MAX_INPUT_BYTES = 200 * 1024 * 1024  # 200 MB safety cap for processing on the bot host


class VideoMetadataError(ClipAssemblyError):
    pass


@dataclass
class VariationParams:
    zoom: float
    pan_x_ratio: float
    pan_y_ratio: float
    rotation_deg: float
    saturation: float
    contrast: float
    brightness: float
    noise_level: float
    speed: float
    start_trim: float
    end_trim: float
    crf: int
    preset: str
    audio_volume: float


@dataclass
class CleanResult:
    output_path: Path
    tags_removed: list[str]
    size_before: int
    size_after: int
    duration_before: float
    duration_after: float
    variation: VariationParams
    has_audio: bool


def _run(args: list[str], *, timeout: int = 900) -> None:
    ffmpeg_bin = resolve_ffmpeg_bin()
    if not ffmpeg_bin:
        raise VideoMetadataError("FFmpeg introuvable sur la machine du bot.")
    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        subprocess.run(cmd, check=True, timeout=timeout, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace")[-1000:]
        raise VideoMetadataError(f"FFmpeg a échoué : {stderr}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoMetadataError("FFmpeg a expiré (vidéo trop longue ?).") from exc


def probe_json(path: Path) -> dict:
    ffprobe = resolve_ffprobe_bin()
    if not ffprobe:
        raise VideoMetadataError("ffprobe introuvable sur la machine du bot.")
    proc = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise VideoMetadataError(f"ffprobe a échoué : {proc.stderr[-500:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise VideoMetadataError("Impossible de lire les infos de la vidéo.") from exc


def collect_identity_tags(probe: dict) -> dict[str, str]:
    """Flatten format + per-stream tags into {tag_name: value} for display."""
    tags: dict[str, str] = {}
    fmt_tags = probe.get("format", {}).get("tags", {}) or {}
    for key, value in fmt_tags.items():
        tags[key] = str(value)
    for stream in probe.get("streams", []) or []:
        stream_tags = stream.get("tags", {}) or {}
        prefix = f"stream#{stream.get('index', '?')}:{stream.get('codec_type', '?')}"
        for key, value in stream_tags.items():
            tags[f"{prefix}.{key}"] = str(value)
    return tags


def _video_stream(probe: dict) -> dict | None:
    for stream in probe.get("streams", []) or []:
        if stream.get("codec_type") == "video":
            return stream
    return None


def _has_audio(probe: dict) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe.get("streams", []) or [])


def _duration(probe: dict) -> float:
    raw = probe.get("format", {}).get("duration")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _random_variation(rng: random.Random) -> VariationParams:
    return VariationParams(
        zoom=rng.uniform(1.015, 1.05),
        pan_x_ratio=rng.uniform(0.0, 1.0),
        pan_y_ratio=rng.uniform(0.0, 1.0),
        rotation_deg=rng.uniform(-0.45, 0.45),
        saturation=rng.uniform(0.93, 1.09),
        contrast=rng.uniform(0.95, 1.06),
        brightness=rng.uniform(-0.035, 0.035),
        noise_level=rng.uniform(1.0, 3.0),
        speed=rng.uniform(0.985, 1.015),
        start_trim=rng.uniform(0.0, 0.08),
        end_trim=rng.uniform(0.04, 0.15),
        crf=rng.choice([19, 20, 21, 22, 23]),
        preset=rng.choice(["medium", "fast", "faster", "veryfast"]),
        audio_volume=rng.uniform(0.97, 1.03),
    )


def _build_filters(
    variation: VariationParams,
    *,
    width: int,
    height: int,
    duration: float,
    has_audio: bool,
) -> tuple[str, str | None]:
    zoom_w = max(width, int(width * variation.zoom))
    zoom_h = max(height, int(height * variation.zoom))
    max_pan_x = max(0, zoom_w - width)
    max_pan_y = max(0, zoom_h - height)
    pan_x = int(max_pan_x * variation.pan_x_ratio)
    pan_y = int(max_pan_y * variation.pan_y_ratio)
    rot_rad = variation.rotation_deg * 3.14159265 / 180.0

    trim_end = max(variation.start_trim + 0.2, duration - variation.end_trim)

    video_filters = [
        f"trim=start={variation.start_trim:.3f}:end={trim_end:.3f}",
        "setpts=PTS-STARTPTS",
        (
            f"eq=saturation={variation.saturation:.4f}:"
            f"contrast={variation.contrast:.4f}:"
            f"brightness={variation.brightness:.4f}"
        ),
        f"rotate={rot_rad:.5f}:fillcolor=black@0:ow=iw:oh=ih",
        f"scale={zoom_w}:{zoom_h}",
        f"crop={width}:{height}:{pan_x}:{pan_y}",
        f"noise=alls={variation.noise_level:.2f}:allf=t+u",
        f"setpts={1 / variation.speed:.6f}*PTS",
    ]
    vf = ",".join(video_filters)

    af = None
    if has_audio:
        audio_filters = [
            f"atrim=start={variation.start_trim:.3f}:end={trim_end:.3f}",
            "asetpts=PTS-STARTPTS",
            f"atempo={variation.speed:.4f}",
            f"volume={variation.audio_volume:.4f}",
            "aresample=44100",
        ]
        af = ",".join(audio_filters)

    return vf, af


def clean_and_vary_video(
    input_path: Path,
    output_path: Path | None = None,
    *,
    seed: int | None = None,
    apply_variation: bool = True,
) -> CleanResult:
    """
    Strip identity metadata and (optionally) apply subtle re-encode
    variations. Always re-encodes (never stream-copies) so the encoder
    signature also changes.
    """
    if not ffmpeg_available():
        raise VideoMetadataError(
            "FFmpeg introuvable. Installe FFmpeg ou configure FFMPEG_PATH dans .env."
        )
    if not input_path.is_file():
        raise VideoMetadataError("Fichier vidéo introuvable.")

    size_before = input_path.stat().st_size
    if size_before > MAX_INPUT_BYTES:
        raise VideoMetadataError(
            f"Vidéo trop lourde ({size_before // (1024 * 1024)} Mo, max "
            f"{MAX_INPUT_BYTES // (1024 * 1024)} Mo)."
        )

    probe = probe_json(input_path)
    tags = collect_identity_tags(probe)
    duration = _duration(probe)
    video_stream = _video_stream(probe) or {}
    width = int(video_stream.get("width") or 1080)
    height = int(video_stream.get("height") or 1920)
    has_audio = _has_audio(probe)

    rng = random.Random(seed)
    variation = _random_variation(rng)

    if output_path is None:
        output_path = input_path.parent / f"clean_{uuid.uuid4().hex}.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    args = ["-i", str(input_path)]

    if apply_variation and duration > (variation.start_trim + variation.end_trim + 0.3):
        vf, af = _build_filters(
            variation, width=width, height=height, duration=duration, has_audio=has_audio
        )
        args += ["-vf", vf]
        if af:
            args += ["-af", af]
    else:
        # Duration too short for trim-based variation — metadata-only clean.
        args += ["-map", "0"]

    args += [
        "-c:v", "libx264",
        "-preset", variation.preset,
        "-crf", str(variation.crf),
        "-pix_fmt", "yuv420p",
    ]
    if has_audio:
        args += ["-c:a", "aac", "-b:a", "128k"]

    # Strip every container/stream metadata field + chapters + disposition.
    args += [
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-metadata", "creation_time=",
        "-metadata", "comment=",
        "-metadata", "title=",
        "-metadata", "description=",
        "-metadata", "copyright=",
        "-metadata", "artist=",
        "-metadata", "encoder=",
        "-metadata", "location=",
        "-metadata", "location-eng=",
        "-metadata:s:v:0", "handler_name=",
        "-metadata:s:v:0", "encoder=",
    ]
    if has_audio:
        args += ["-metadata:s:a:0", "handler_name=", "-metadata:s:a:0", "encoder="]

    args += [
        "-movflags", "+faststart",
        "-fflags", "+bitexact",
        "-flags:v", "+bitexact",
        str(output_path),
    ]

    try:
        _run(args)
    except VideoMetadataError:
        output_path.unlink(missing_ok=True)
        raise

    if not output_path.is_file() or output_path.stat().st_size < 1024:
        output_path.unlink(missing_ok=True)
        raise VideoMetadataError("Le rendu a produit un fichier vide.")

    out_probe = probe_json(output_path)

    return CleanResult(
        output_path=output_path,
        tags_removed=sorted(tags.keys()),
        size_before=size_before,
        size_after=output_path.stat().st_size,
        duration_before=duration,
        duration_after=_duration(out_probe),
        variation=variation,
        has_audio=has_audio,
    )


def variation_summary(variation: VariationParams) -> str:
    return (
        f"Zoom {variation.zoom:.1%} · rotation {variation.rotation_deg:+.2f}° · "
        f"saturation {variation.saturation:.0%} · contraste {variation.contrast:.0%} · "
        f"vitesse {variation.speed:.1%} · bruit {variation.noise_level:.1f} · "
        f"encodage crf{variation.crf}/{variation.preset}"
    )
