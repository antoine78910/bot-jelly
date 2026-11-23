"""One source clip in, one guarded unique variant out.

render_unique() encodes up to MAX_ATTEMPTS variants (the initial encode plus
MAX_RETRIES retries). Each attempt is measured against the original and against
every retained sibling (earlier deliveries of the same source). The first attempt
that passes every guard is returned; rejected files are deleted at once. If all
attempts fail, GuardFailed is raised and nothing is left on disk.

Fail closed: missing audio, a fingerprint too short to compare, or any NaN/inf
metric counts as a failed guard, never as a pass.
"""

from __future__ import annotations

import hashlib
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

import numpy as np

from . import media, metrics
from .frames import CANVAS_H, CANVAS_W, TEMPLATES, VariantParams, render_frame, sample_params

PHASH_MIN_VS_ORIG = 16
SSIM_MAX_VS_ORIG = 0.5
CHROMA_MAX_VS_ORIG = 0.20
PHASH_MIN_VS_SIBLING = 10
CHROMA_MAX_VS_SIBLING = 0.30
MAX_RETRIES = 3
MAX_ATTEMPTS = 1 + MAX_RETRIES

VISUAL_FRACS = (0.08, 0.25, 0.50, 0.75, 0.92)
# Sibling frames are taken at the same SOURCE instants in both renders, so a
# different speed or trim does not make two siblings look different by accident.
SIBLING_SOURCE_FRACS = (0.25, 0.50, 0.75)

THRESHOLDS = {
    "phash_min_vs_original": PHASH_MIN_VS_ORIG,
    "ssim_max_vs_original": SSIM_MAX_VS_ORIG,
    "chroma_max_vs_original": CHROMA_MAX_VS_ORIG,
    "phash_min_vs_sibling": PHASH_MIN_VS_SIBLING,
    "chroma_max_vs_sibling": CHROMA_MAX_VS_SIBLING,
    "max_attempts": MAX_ATTEMPTS,
}


class GuardFailed(Exception):
    """No attempt passed every guard. .report holds every attempt's checks."""

    def __init__(self, report: dict) -> None:
        super().__init__("no variant passed the uniqueness guards")
        self.report = report


@dataclass
class Signature:
    """What a delivered variant leaves behind so later siblings can be compared to it."""

    ref: str
    source_fracs: list[float]
    phash: list[str]  # 64-bit hex, one per SIBLING_SOURCE_FRACS
    fingerprint: list[int]
    pitch_sign: int = 0
    template: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Signature":
        sig = cls(
            ref=str(data["ref"]),
            source_fracs=[float(x) for x in data["source_fracs"]],
            phash=[str(x) for x in data["phash"]],
            fingerprint=[int(x) for x in data["fingerprint"]],
            pitch_sign=int(data.get("pitch_sign", 0)),
            template=str(data.get("template", "")),
        )
        if len(sig.phash) != len(sig.source_fracs):
            raise ValueError("signature phash/fracs mismatch")
        for value in sig.phash:
            metrics.hex_to_bits(value)
        return sig


@dataclass
class RenderResult:
    output: Path
    params: VariantParams
    signature: Signature
    report: dict = field(default_factory=dict)


def ffmpeg_threads() -> int:
    raw = os.getenv("VIDEO_FFMPEG_THREADS", "").strip()
    return max(1, min(8, int(raw))) if raw.isdigit() else 2


def encode_timeout(duration: float) -> float:
    raw = os.getenv("VIDEO_ENCODE_TIMEOUT", "").strip()
    if raw.isdigit():
        return float(raw)
    return float(min(1800, max(300, duration * 8)))


def attempt_seed(seed: int, attempt: int) -> int:
    if attempt == 0:
        return seed
    digest = hashlib.sha256(f"{seed}:{attempt}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def template_for(seed: int, attempt: int) -> str:
    return TEMPLATES[(seed + attempt) % len(TEMPLATES)]


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------

def build_filter(params: VariantParams) -> str:
    trim, speed = params.trim, params.speed
    vw, vh = params.video_w, params.video_h
    x, y = params.video_x, params.video_y
    video_src = (
        f"[0:v]trim=start={trim},setpts=(PTS-STARTPTS)/{speed},split=2[vsrc][vblur];"
        f"[vsrc]scale={vw}:{vh}:force_original_aspect_ratio=increase:flags=lanczos,crop={vw}:{vh},"
        f"eq=brightness={params.brightness}:saturation={params.saturation},format=rgba[vid];"
    )
    if params.bg_mode == "blur":
        bg = (
            f"[vblur]scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=increase:flags=fast_bilinear,"
            f"crop={CANVAS_W}:{CANVAS_H},scale=270:480,boxblur=16:8,"
            f"scale={CANVAS_W}:{CANVAS_H}:flags=fast_bilinear,"
            "eq=brightness=-0.28:saturation=0.72,format=rgba[bg];"
        )
    else:
        bg = f"color=c={params.bg_color}:s={CANVAS_W}x{CANVAS_H}:r=60,format=rgba[bg];[vblur]nullsink;"
    compose = (
        f"[bg][vid]overlay={x}:{y}[tmp];"
        "[tmp][1:v]overlay=0:0:shortest=1,fps=60,setsar=1,format=yuv420p[vo];"
    )
    # rubberband only: asetrate+atempo is a weaker pitch change and is not allowed.
    audio = (
        f"[0:a]atrim=start={trim},asetpts=PTS-STARTPTS,"
        f"rubberband=tempo={speed}:pitch={params.pitch_ratio},aresample=48000[ao]"
    )
    return video_src + bg + compose + audio


def encode_command(src: Path, frame_png: Path, params: VariantParams, dest: Path, threads: int) -> list[str]:
    ffmpeg = media.ffmpeg_bin()
    if not ffmpeg:
        raise media.MediaError("Outil `ffmpeg` introuvable sur le serveur.")
    return [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-threads", str(threads),
        "-i", str(src),
        "-loop", "1", "-i", str(frame_png),
        "-filter_complex_threads", str(threads),
        "-filter_complex", build_filter(params),
        "-map", "[vo]", "-map", "[ao]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p", "-r", "60",
        "-threads", str(threads),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-movflags", "+faststart",
        "-shortest",
        str(dest),
    ]


def encode(
    src: Path,
    frame_png: Path,
    params: VariantParams,
    dest: Path,
    *,
    duration: float,
    threads: int,
    cancel: threading.Event | None,
) -> None:
    code, _out, err = media.run_cmd(
        encode_command(src, frame_png, params, dest, threads),
        timeout=encode_timeout(duration),
        cancel=cancel,
    )
    if code != 0:
        tail = err.decode("utf-8", "replace")[-600:]
        raise media.MediaError(f"Encodage ffmpeg échoué : {tail.strip() or code}")


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _check(name: str, value, limit: float, op: str, **extra) -> dict:
    finite = value is not None and isinstance(value, (int, float)) and math.isfinite(value)
    if not finite:
        ok = False
    elif op == ">=":
        ok = value >= limit
    else:
        ok = value <= limit
    shown = round(float(value), 4) if finite else None
    return {"name": name, "value": shown, "limit": limit, "op": op, "ok": ok, **extra}


def _clamp(t: float, duration: float) -> float:
    return max(0.05, min(duration - 0.08, t))


def sibling_bits(path: Path, params: VariantParams, src_duration: float, var_duration: float, cancel) -> list[np.ndarray]:
    bits = []
    for frac in SIBLING_SOURCE_FRACS:
        t_src = frac * src_duration
        t_var = _clamp((t_src - params.trim) / params.speed, var_duration)
        bits.append(metrics.phash_bits(media.grab_frame(path, t_var, cancel=cancel)))
    return bits


def evaluate(
    src: Path,
    variant: Path,
    params: VariantParams,
    *,
    src_duration: float,
    var_duration: float,
    src_fp: np.ndarray,
    var_fp: np.ndarray,
    var_bits: list[np.ndarray],
    siblings: Sequence[Signature],
    cancel: threading.Event | None = None,
) -> dict:
    samples = []
    for frac in VISUAL_FRACS:
        t_var = _clamp(frac * var_duration, var_duration)
        t_orig = _clamp(t_var * params.speed + params.trim, src_duration)
        a = media.grab_frame(src, t_orig, cancel=cancel)
        b = media.grab_frame(variant, t_var, cancel=cancel)
        samples.append({
            "t_var": round(t_var, 3),
            "t_orig": round(t_orig, 3),
            "phash": metrics.phash_dist(a, b),
            "ssim": round(metrics.fullframe_ssim(a, b), 4),
        })
    phash_mean = float(np.mean([s["phash"] for s in samples]))
    ssim_mean = float(np.mean([s["ssim"] for s in samples]))
    chroma = metrics.chromaprint_similarity(src_fp, var_fp)
    checks = [
        _check("phash_mean_vs_original", phash_mean, PHASH_MIN_VS_ORIG, ">="),
        _check("ssim_mean_vs_original", ssim_mean, SSIM_MAX_VS_ORIG, "<="),
        _check("chromaprint_vs_original", chroma, CHROMA_MAX_VS_ORIG, "<="),
    ]
    sibling_rows = []
    for sib in siblings:
        dists = [
            metrics.bits_distance(metrics.hex_to_bits(h), bits)
            for h, bits in zip(sib.phash, var_bits)
        ]
        ph = float(np.mean(dists)) if len(dists) == len(var_bits) else None
        ch = metrics.chromaprint_similarity(np.asarray(sib.fingerprint, dtype=np.uint32), var_fp)
        pair = [
            _check("phash_mean_vs_sibling", ph, PHASH_MIN_VS_SIBLING, ">=", sibling=sib.ref),
            _check("chromaprint_vs_sibling", ch, CHROMA_MAX_VS_SIBLING, "<=", sibling=sib.ref),
        ]
        sibling_rows.append({"sibling": sib.ref, "phash_dists": dists})
        checks.extend(pair)
    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "visual_samples": samples,
        "sibling_samples": sibling_rows,
        "siblings_compared": len(siblings),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _params_summary(params: VariantParams) -> dict:
    data = asdict(params)
    data.pop("account", None)
    data.pop("quoter", None)
    return data


def render_unique(
    src: Path,
    work_dir: Path,
    *,
    seed: int,
    pitch_sign: int,
    siblings: Sequence[Signature] = (),
    ref: str = "",
    cancel: threading.Event | None = None,
    today: date | None = None,
) -> RenderResult:
    """Blocking. Run it in a worker thread, never on the event loop."""
    started = time.monotonic()
    problems = media.selfcheck()
    if problems:
        raise media.MediaError("Outils vidéo manquants : " + "; ".join(problems))
    work_dir.mkdir(parents=True, exist_ok=True)
    info = media.source_info(media.probe(src, cancel=cancel))
    src_fp = media.fingerprint(src, cancel=cancel)
    if src_fp.size < metrics.MIN_FP_ITEMS:
        raise media.MediaError("Audio trop court ou silencieux pour l’empreinte sonore.")

    threads = ffmpeg_threads()
    report: dict = {
        "thresholds": THRESHOLDS,
        "source_duration": round(info.duration, 3),
        "pitch_sign": pitch_sign,
        "siblings_retained": len(siblings),
        "threads": threads,
        "attempts": [],
    }
    for attempt in range(MAX_ATTEMPTS):
        attempt_started = time.monotonic()
        aseed = attempt_seed(seed, attempt)
        params = sample_params(
            f"attempt{attempt}", aseed, template_for(seed, attempt), attempt,
            pitch_sign=pitch_sign, today=today,
        )
        frame = work_dir / f"frame_{attempt}.png"
        out = work_dir / f"attempt_{attempt}.mp4"
        entry: dict = {"attempt": attempt, "params": _params_summary(params)}
        try:
            render_frame(params, frame)
            encode(src, frame, params, out, duration=info.duration, threads=threads, cancel=cancel)
            out_probe = media.probe(out, cancel=cancel)
            format_problems = media.output_problems(out_probe)
            var_duration = float(out_probe["format"]["duration"])
            var_fp = media.fingerprint(out, cancel=cancel)
            var_bits = sibling_bits(out, params, info.duration, var_duration, cancel)
            result = evaluate(
                src, out, params,
                src_duration=info.duration, var_duration=var_duration,
                src_fp=src_fp, var_fp=var_fp, var_bits=var_bits,
                siblings=siblings, cancel=cancel,
            )
            result["checks"].insert(0, {
                "name": "output_format", "value": "; ".join(format_problems) or "ok",
                "limit": "1080x1920 h264 60fps aac 48k, no tags", "op": "==",
                "ok": not format_problems,
            })
            result["ok"] = result["ok"] and not format_problems
            entry.update(result, duration=round(var_duration, 3))
        except media.MediaError as exc:
            entry.update(ok=False, error=str(exc))
            result = {"ok": False}
        except BaseException:
            out.unlink(missing_ok=True)  # cancelled or crashed: no partial file left
            raise
        finally:
            frame.unlink(missing_ok=True)
        entry["elapsed_s"] = round(time.monotonic() - attempt_started, 2)
        report["attempts"].append(entry)
        if result["ok"]:
            report["accepted_attempt"] = attempt
            report["elapsed_s"] = round(time.monotonic() - started, 2)
            signature = Signature(
                ref=ref,
                source_fracs=list(SIBLING_SOURCE_FRACS),
                phash=[metrics.bits_to_hex(b) for b in var_bits],
                fingerprint=[int(x) for x in var_fp],
                pitch_sign=pitch_sign,
                template=params.template,
            )
            return RenderResult(output=out, params=params, signature=signature, report=report)
        out.unlink(missing_ok=True)  # never leak a rejected render
    report["elapsed_s"] = round(time.monotonic() - started, 2)
    raise GuardFailed(report)
