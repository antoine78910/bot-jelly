#!/usr/bin/env python3
"""End-to-end preview of the Vidéo button with REAL encodes, guards and hosting.

Discord is simulated (offline doubles from tests/discord_fakes.py): no bot token,
no Discord network, no OpenAI. Everything else is the production code path:

  staff /add-video (real ffprobe validation)
  -> 3 creators click « Vidéo » (creator gate, private thread, reservation, queue)
  -> worker: render_unique (ffmpeg + rubberband, pHash/SSIM/chromaprint guards)
  -> publish on the bot's own aiohttp app (/v/<token>, served on 127.0.0.1)
  -> simulated thread send -> assignment marked delivered

Then each link is downloaded over HTTP, a contact sheet is drawn and every guard
value is written to preview.json. Exit code 0 only if all 3 were delivered and
every guard passed.

  python scripts/preview_videos.py --src /workspace/dedup-src/original.MOV --out /workspace/bot-jelly-preview
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "tests")]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _redact(text: str) -> str:
    return re.sub(r"/v/[A-Za-z0-9_-]{20,}", "/v/<token>", text or "")


def contact_sheet(original: Path, outputs: list[tuple[str, Path]], dest: Path) -> None:
    from PIL import Image, ImageDraw

    from video_uniquify import media
    from video_uniquify.chrome import inter

    font = inter(18, 700)
    fracs = (0.08, 0.50, 0.92)
    tw, th, gap = 270, 480, 8
    columns = [("ORIGINAL", original)] + outputs
    sheet = Image.new("RGB", (len(columns) * (tw + gap) + gap, len(fracs) * (th + gap) + gap), (20, 20, 20))
    for c, (label, path) in enumerate(columns):
        duration = float(media.probe(path)["format"]["duration"])
        for r, frac in enumerate(fracs):
            t = max(0.05, min(duration - 0.08, frac * duration))
            im = Image.fromarray(media.grab_frame(path, t)).resize((tw, th), Image.LANCZOS)
            d = ImageDraw.Draw(im)
            d.rectangle((0, 0, tw, 30), fill=(0, 0, 0))
            d.text((6, 5), f"{label} t={t:.1f}s", fill=(255, 255, 0), font=font)
            sheet.paste(im, (gap + c * (tw + gap), gap + r * (th + gap)))
    sheet.save(dest, "JPEG", quality=90)


def pairwise(outputs: list[dict], src_duration: float) -> list[dict]:
    """Independent re-measure between delivered outputs, aligned on source time."""
    import numpy as np

    from video_uniquify import media, metrics, pipeline

    prints = {o["name"]: media.fingerprint(Path(o["path"])) for o in outputs}
    bits = {}
    for o in outputs:
        p = o["params"]
        duration = float(media.probe(Path(o["path"]))["format"]["duration"])
        row = []
        for frac in pipeline.SIBLING_SOURCE_FRACS:
            t = max(0.05, min(duration - 0.08, (frac * src_duration - p["trim"]) / p["speed"]))
            row.append(metrics.phash_bits(media.grab_frame(Path(o["path"]), t)))
        bits[o["name"]] = row
    rows = []
    for i, a in enumerate(outputs):
        for b in outputs[i + 1:]:
            ph = float(np.mean([metrics.bits_distance(x, y) for x, y in zip(bits[a["name"]], bits[b["name"]])]))
            ch = metrics.chromaprint_similarity(prints[a["name"]], prints[b["name"]])
            rows.append({
                "a": a["name"], "b": b["name"],
                "phash_mean": round(ph, 2), "phash_ok": ph >= pipeline.PHASH_MIN_VS_SIBLING,
                "chromaprint": None if ch is None else round(ch, 4),
                "chromaprint_ok": ch is not None and ch <= pipeline.CHROMA_MAX_VS_SIBLING,
            })
    return rows


async def run(src: Path, out: Path, clippers: int, title: str) -> int:
    out.mkdir(parents=True, exist_ok=True)
    data_dir = out / "data"
    port = _free_port()
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["PUBLIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "FAL_KEY", "SPEEL_API_URL", "PERSONA_API_SECRET"):
        os.environ.pop(key, None)  # nothing in this path may reach an AI API

    from unittest import mock

    import aiohttp
    from aiohttp import web

    import content_generator
    import persona
    import video_generator
    import video_hosting
    from creator_signup_webhook import create_webhook_app
    from discord_fakes import FakeGuild, FakeInteraction, make_attachment, make_channel, make_member, view_urls
    from registration import creator_role_id
    from video_store import get_store
    from video_uniquify import media

    started = time.monotonic()
    problems = media.selfcheck()
    if problems:
        print("selfcheck failed: " + "; ".join(problems), file=sys.stderr)
        return 2

    app = create_webhook_app(bot=None)
    runner = web.AppRunner(app, access_log_class=video_hosting.RedactingAccessLogger)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()

    reports: dict[str, dict] = {}
    timings: dict[str, dict] = {}
    real_run_render = video_generator.run_render
    real_process = video_generator.process_job

    async def recording_render(job, *args, **kwargs):
        t0 = time.monotonic()
        try:
            result = await real_run_render(job, *args, **kwargs)
            reports[job.assignment_id] = result.report
            return result
        except Exception as exc:
            reports[job.assignment_id] = getattr(exc, "report", {"error": repr(exc)})
            raise
        finally:
            timings.setdefault(job.assignment_id, {})["render_s"] = round(time.monotonic() - t0, 2)

    async def timed_process(job):
        t0 = time.monotonic()
        try:
            await real_process(job)
        finally:
            timings.setdefault(job.assignment_id, {}).update(
                job_s=round(time.monotonic() - t0, 2),
                queue_wait_s=round(max(0.0, time.time() - job.enqueued_at - (time.monotonic() - t0)), 2),
                outcome=job.outcome,
            )

    summary: dict = {"source": str(src), "out": str(out), "clippers": []}
    patches = [
        mock.patch.object(persona, "is_configured", return_value=False),
        mock.patch.object(persona, "_is_approved_creator", return_value=False),
        mock.patch.object(video_generator, "run_render", side_effect=recording_render),
        mock.patch.object(video_generator, "process_job", side_effect=timed_process),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        video_generator._queue = None
        await video_generator.start()
        store = get_store()
        summary["store"] = {"root": str(store.root), "persistent": store.persistent, "warning": store.warning}

        guild = FakeGuild()
        channel = make_channel(guild)
        staff = make_member(guild, name="staff-preview", admin=True)
        t0 = time.monotonic()
        add = FakeInteraction(staff, channel)
        await video_generator.add_video_command.callback(add, video=make_attachment(src), titre=title)
        summary["add_video"] = {"replies": add.texts(), "elapsed_s": round(time.monotonic() - t0, 2)}
        if not any("Vidéo ajoutée" in t for t in add.texts()):
            print("add-video refused: " + " | ".join(add.texts()), file=sys.stderr)
            await video_generator.stop()
            await runner.cleanup()
            (out / "preview.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            return 1

        members = [make_member(guild, name=f"clipper{i + 1}", roles=(creator_role_id(),)) for i in range(clippers)]
        view = content_generator.ContentGeneratorView()
        button = next(i for i in view.children if getattr(i, "custom_id", "") == "jelly_content_video")
        interactions = []
        for member in members:
            interaction = FakeInteraction(member, channel)
            await button.callback(interaction)
            interactions.append(interaction)
        await video_generator.get_queue().drain()
        await video_generator.stop()

        source = next(v for v in store.list_videos() if v.get("active"))
        assignments = {a["clipper_id"]: a for a in store._load()["assignments"]}
        outputs = []
        async with aiohttp.ClientSession() as http:
            for index, (member, interaction) in enumerate(zip(members, interactions), start=1):
                thread = next((t for t in channel.created if t.name == content_generator.thread_name_for(member)), None)
                a = assignments.get(member.id, {})
                entry = {
                    "name": f"clipper{index}",
                    "assignment": a.get("id"),
                    "status": a.get("status"),
                    "reason": a.get("reason"),
                    "pitch_sign": a.get("pitch_sign"),
                    "pitch_slot": a.get("pitch_slot"),
                    "cycle": a.get("cycle"),
                    "ephemeral": [_redact(t) for t in interaction.texts()],
                    "thread_messages": [_redact(m.get("content") or "") for m in (thread.sent if thread else [])],
                    "timings": timings.get(a.get("id"), {}),
                    "report": reports.get(a.get("id"), {}),
                }
                delivered = [m for m in (thread.sent if thread else []) if m.get("view") is not None]
                if delivered:
                    url = view_urls(delivered[-1]["view"])[0]
                    dest = out / f"clipper{index}.mp4"
                    async with http.get(url) as page:
                        entry["http_page_status"] = page.status
                    async with http.get(f"{url}/video.mp4?dl=1") as resp:
                        entry["http_download_status"] = resp.status
                        entry["content_disposition"] = resp.headers.get("Content-Disposition")
                        dest.write_bytes(await resp.read())
                    entry["path"] = str(dest)
                    entry["size_mb"] = round(dest.stat().st_size / 1e6, 2)
                    accepted = entry["report"]["attempts"][entry["report"]["accepted_attempt"]]
                    entry["params"] = accepted["params"]
                    entry["checks"] = accepted["checks"]
                    entry["all_guards_passed"] = all(c["ok"] for c in accepted["checks"])
                    entry["output_format_problems"] = media.output_problems(media.probe(dest))
                    outputs.append(entry)
                summary["clippers"].append(entry)
        await runner.cleanup()

    if outputs:
        contact_sheet(src, [(o["name"].upper(), Path(o["path"])) for o in outputs], out / "contact_sheet.jpg")
        summary["contact_sheet"] = str(out / "contact_sheet.jpg")
        summary["pairwise_outputs"] = pairwise(outputs, float(source["duration"]))
    summary["elapsed_s"] = round(time.monotonic() - started, 2)
    ok = (
        len(outputs) == clippers
        and all(o["all_guards_passed"] and not o["output_format_problems"] for o in outputs)
        and all(o.get("http_download_status") == 200 for o in outputs)
        and len({o["pitch_sign"] for o in outputs}) == (2 if clippers > 1 else 1)
    )
    summary["ok"] = ok
    (out / "preview.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": ok,
        "delivered": len(outputs),
        "elapsed_s": summary["elapsed_s"],
        "outputs": [o.get("path") for o in outputs],
        "metrics": str(out / "preview.json"),
    }, indent=2))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", type=Path, required=True, help="9:16 source clip with audio, 180 s max")
    parser.add_argument("--out", type=Path, required=True, help="output folder (data, mp4s, sheet, json)")
    parser.add_argument("--clippers", type=int, default=3)
    parser.add_argument("--title", default="Preview source")
    args = parser.parse_args(argv)
    if not args.src.is_file():
        print(f"source not found: {args.src}", file=sys.stderr)
        return 2
    return asyncio.run(run(args.src.resolve(), args.out.resolve(), max(1, args.clippers), args.title))


if __name__ == "__main__":
    sys.exit(main())
