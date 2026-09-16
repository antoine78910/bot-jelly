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

Optionally (inject_iphone_signature), the cleaned file can also carry a
plausible "just recorded on an iPhone 15 Pro" signature (make/model/iOS
version + a creation date a few minutes ago) instead of empty fields —
same idea as the visual variation: every export looks like its own
distinct, freshly-shot clip rather than a stripped/edited file.

Fields known to carry identity in MP4/MOV containers (see docstring at
IDENTITY_FIELDS_INFO below for the full research summary).
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

# "Freshly recorded on iPhone 15 Pro" signature — calibrated against real
# iPhone 15 Pro / iOS 26.3 exports in samples/ (make/model/software/
# creationdate/handlers/encoder/language). GPS intentionally omitted.
IPHONE_MAKE = "Apple"
IPHONE_MODEL = "iPhone 15 Pro"
IPHONE_SOFTWARE = "26.3"


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
    hue_deg: float
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


def _probe_via_ffprobe(path: Path) -> dict | None:
    """Preferred path — structured JSON. Returns None if ffprobe isn't installed."""
    ffprobe = resolve_ffprobe_bin()
    if not ffprobe:
        return None
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
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


_STREAM_HEADER_RE = re.compile(
    r"^\s*Stream #\d+:\d+.*?:\s*(Video|Audio|Subtitle|Data)\b(.*)$"
)
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_DIMENSIONS_RE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_TAG_LINE_RE = re.compile(r"^(\s+)([A-Za-z0-9_.\- ]+?)\s*:\s?(.*)$")


def _probe_via_ffmpeg_stderr(path: Path) -> dict:
    """
    Fallback probing that only needs the `ffmpeg` binary (no ffprobe).
    Parses the human-readable input analysis ffmpeg prints to stderr for
    any `-i` invocation. Used automatically when ffprobe isn't installed
    (e.g. the `imageio-ffmpeg` PyPI package only ships ffmpeg, not ffprobe).
    """
    ffmpeg_bin = resolve_ffmpeg_bin()
    if not ffmpeg_bin:
        raise VideoMetadataError("FFmpeg introuvable sur la machine du bot.")

    proc = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    stderr = proc.stderr or ""
    lines = stderr.splitlines()

    format_tags: dict[str, str] = {}
    streams: list[dict] = []
    duration = 0.0

    scope: str | int | None = None
    in_metadata = False
    metadata_indent = 0

    for line in lines:
        stripped = line.strip()

        # Stop before ffmpeg re-declares streams/tags for the throwaway
        # `-f null -` output — we only want the *input* file's own info.
        if stripped.startswith("Output #") or stripped.startswith("Stream mapping"):
            break

        if stripped == "Metadata:":
            in_metadata = True
            metadata_indent = len(line) - len(line.lstrip(" "))
            continue

        if in_metadata:
            indent = len(line) - len(line.lstrip(" "))
            match = _TAG_LINE_RE.match(line)
            if match and indent > metadata_indent:
                key = match.group(2).strip()
                value = match.group(3).strip()
                if scope == "format" or scope is None:
                    format_tags[key] = value
                elif isinstance(scope, int) and 0 <= scope < len(streams):
                    streams[scope]["tags"][key] = value
                continue
            in_metadata = False

        if stripped.startswith("Input #"):
            scope = "format"

        dur_match = _DURATION_RE.search(line)
        if dur_match and duration == 0.0:
            hours, minutes, seconds = dur_match.groups()
            duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

        stream_match = _STREAM_HEADER_RE.match(line)
        if stream_match:
            codec_type = stream_match.group(1).lower()
            rest = stream_match.group(2)
            width = height = None
            if codec_type == "video":
                dim_match = _DIMENSIONS_RE.search(rest)
                if dim_match:
                    width, height = int(dim_match.group(1)), int(dim_match.group(2))
            streams.append(
                {
                    "index": len(streams),
                    "codec_type": codec_type,
                    "width": width,
                    "height": height,
                    "tags": {},
                }
            )
            scope = len(streams) - 1

    if not streams and "No such file" in stderr:
        raise VideoMetadataError("Fichier vidéo introuvable ou illisible.")

    return {
        "format": {"duration": str(duration), "tags": format_tags},
        "streams": streams,
    }


def probe_json(path: Path) -> dict:
    """
    Probe a media file's format/streams/tags. Tries ffprobe first (richer,
    exact JSON); falls back to parsing `ffmpeg -i` stderr when ffprobe isn't
    installed on the host (e.g. Railway with only imageio-ffmpeg's bundled
    ffmpeg binary).
    """
    result = _probe_via_ffprobe(path)
    if result is not None:
        return result
    return _probe_via_ffmpeg_stderr(path)


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
        zoom=rng.uniform(1.02, 1.06),
        pan_x_ratio=rng.uniform(0.0, 1.0),
        pan_y_ratio=rng.uniform(0.0, 1.0),
        rotation_deg=rng.uniform(-0.45, 0.45),
        saturation=rng.uniform(0.9, 1.12),
        contrast=rng.uniform(0.94, 1.08),
        brightness=rng.uniform(-0.05, 0.05),
        hue_deg=rng.uniform(-6.0, 6.0),
        noise_level=rng.uniform(1.0, 3.0),
        speed=rng.uniform(0.985, 1.015),
        start_trim=rng.uniform(0.05, 0.2),
        end_trim=rng.uniform(0.1, 0.35),
        crf=rng.choice([19, 20, 21, 22, 23]),
        preset=rng.choice(["medium", "fast", "faster", "veryfast"]),
        audio_volume=rng.uniform(0.97, 1.03),
    )


def _iphone_signature_metadata(rng: random.Random) -> dict[str, str]:
    """
    Build a plausible "just recorded on an iPhone 15 Pro" metadata set.
    Field names/shapes match real Apple Camera .MOV exports (see samples/).
    The recording moment is a few minutes before "now" (export delay).
    """
    now_local = datetime.now().astimezone()
    recorded_local = now_local - timedelta(seconds=rng.uniform(25, 360))
    recorded_utc = recorded_local.astimezone(timezone.utc)
    # Real iPhones write creationdate like 2026-09-16T10:44:11+0700
    # (offset without colon). Python %z already matches that on Windows.
    creationdate = recorded_local.strftime("%Y-%m-%dT%H:%M:%S%z")

    return {
        "creation_time": recorded_utc.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
        "com.apple.quicktime.creationdate": creationdate,
        "com.apple.quicktime.make": IPHONE_MAKE,
        "com.apple.quicktime.model": IPHONE_MODEL,
        "com.apple.quicktime.software": IPHONE_SOFTWARE,
        # Present on every real sample we probed; always 0 for normal clips.
        "com.apple.quicktime.full-frame-rate-playback-intent": "0",
        "major_brand": "qt  ",
        "minor_version": "0",
        "compatible_brands": "qt  ",
    }


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
        f"hue=h={variation.hue_deg:.2f}:s=1",
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
    signature also changes. Output is always a clean, empty-metadata .mp4.

    To make the result look like a fresh iPhone recording instead of a
    metadata-free file, run inject_iphone_signature() on the result — it's
    a separate, fast stream-copy pass so it survives any later Discord-size
    compression (which would otherwise re-mux and drop injected tags).
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


def inject_iphone_signature(
    input_path: Path,
    output_path: Path | None = None,
    *,
    seed: int | None = None,
) -> Path:
    """
    Fast stream-copy remux (no re-encode) that writes a plausible "just
    recorded on an iPhone 15 Pro, iOS 26.3" signature onto an already-clean
    video: make/model/software + a creation date a few minutes ago.

    Output is .mov — the container real iPhone recordings actually use,
    and required for ffmpeg to write the com.apple.quicktime.* keys at
    all (the mp4 muxer silently drops them). Run this as the LAST step,
    after any Discord-size compression, since re-encoding/re-muxing again
    afterwards would drop these tags.
    """
    if not ffmpeg_available():
        raise VideoMetadataError(
            "FFmpeg introuvable. Installe FFmpeg ou configure FFMPEG_PATH dans .env."
        )
    if not input_path.is_file():
        raise VideoMetadataError("Fichier vidéo introuvable.")

    if output_path is None:
        output_path = input_path.with_name(f"{input_path.stem}_iphone.mov")
    elif output_path.suffix.lower() != ".mov":
        output_path = output_path.with_suffix(".mov")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    probe = probe_json(input_path)
    has_audio = _has_audio(probe)

    rng = random.Random(seed)
    signature = _iphone_signature_metadata(rng)

    # Only A/V — real iPhones also embed several Core Media Metadata data
    # tracks we can't recreate with a remux; mapping everything from an
    # intermediate re-encode would just carry ffmpeg leftovers.
    args = ["-i", str(input_path), "-map", "0:v:0", "-c", "copy"]
    if has_audio:
        args += ["-map", "0:a:0"]
    args += ["-map_metadata", "-1", "-map_chapters", "-1"]
    for key, value in signature.items():
        args += ["-metadata", f"{key}={value}"]
    # Match real Camera.app stream tags (samples/IMG_526*.MOV).
    # vendor_id on real iPhones is four NUL bytes — can't pass NULs via
    # Windows argv. With +bitexact the mov muxer may stamp FFMP; without
    # it Lavf leaks into the format encoder tag. Prefer no Lavf.
    args += [
        "-metadata", "encoder=",
        "-metadata:s:v:0", "handler_name=Core Media Video",
        "-metadata:s:v:0", "encoder=H.264",
        "-metadata:s:v:0", "language=und",
    ]
    if has_audio:
        args += [
            "-metadata:s:a:0", "handler_name=Core Media Audio",
            "-metadata:s:a:0", "encoder=",
            "-metadata:s:a:0", "language=und",
        ]
    args += [
        "-fflags", "+bitexact",
        "-brand", "qt  ",
        "-movflags", "+faststart+use_metadata_tags",
        str(output_path),
    ]

    try:
        _run(args, timeout=120)
    except VideoMetadataError:
        output_path.unlink(missing_ok=True)
        raise

    if not output_path.is_file() or output_path.stat().st_size < 1024:
        output_path.unlink(missing_ok=True)
        raise VideoMetadataError("Le remux iPhone a produit un fichier vide.")

    return output_path


def variation_summary(variation: VariationParams) -> str:
    return (
        f"Zoom {variation.zoom:.1%} · rotation {variation.rotation_deg:+.2f}° · "
        f"teinte {variation.hue_deg:+.1f}° · saturation {variation.saturation:.0%} · "
        f"contraste {variation.contrast:.0%} · luminosité {variation.brightness:+.2f} · "
        f"coupe début {variation.start_trim:.2f}s / fin {variation.end_trim:.2f}s · "
        f"vitesse {variation.speed:.1%} · bruit {variation.noise_level:.1f} · "
        f"encodage crf{variation.crf}/{variation.preset}"
    )
