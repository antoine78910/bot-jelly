"""Optional HTTP webhook for Supabase / JobShift creator signup, video, and digest events."""

from __future__ import annotations

import os

from aiohttp import web

from creator_activity import (
    ChannelUnavailable,
    PayloadError,
    forget_video,
    parse_creator_video,
    parse_inactive_report,
    post_creator_video,
    post_inactive_creators,
    remember_video,
)
from creator_signups import (
    CREATOR_VIDEO_WEBHOOK_ENVS,
    handle_application_webhook,
    handle_signup_webhook,
    verify_webhook_secret,
)


def webhook_port() -> int | None:
    raw = os.getenv("CREATOR_SIGNUP_WEBHOOK_PORT", os.getenv("PORT", "")).strip()
    if raw.isdigit():
        return int(raw)
    return None


async def _creator_signup_handler(request: web.Request) -> web.Response:
    if not verify_webhook_secret(dict(request.headers)):
        return web.Response(status=401, text="Unauthorized")

    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    bot = request.app["bot"]
    handled = await handle_signup_webhook(bot, payload)
    if not handled:
        return web.Response(status=202, text="Ignored")

    return web.Response(status=200, text="OK")


async def _creator_application_handler(request: web.Request) -> web.Response:
    if not verify_webhook_secret(dict(request.headers)):
        return web.Response(status=401, text="Unauthorized")

    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    bot = request.app["bot"]
    handled = await handle_application_webhook(bot, payload)
    if not handled:
        return web.Response(status=202, text="Ignored")

    return web.Response(status=200, text="OK")


def _video_secret_ok(request: web.Request) -> bool:
    return verify_webhook_secret(
        dict(request.headers),
        env_names=CREATOR_VIDEO_WEBHOOK_ENVS,
    )


async def _creator_video_handler(request: web.Request) -> web.Response:
    if not _video_secret_ok(request):
        return web.Response(status=401, text="Unauthorized")

    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    if not isinstance(payload, dict):
        return web.Response(status=400, text="Invalid payload")

    try:
        video = parse_creator_video(payload)
    except PayloadError as exc:
        return web.Response(status=400, text=str(exc))

    if not remember_video(video.video_id):
        print(f"Creator video {video.video_id} already posted, skipping")
        return web.Response(status=200, text="OK")

    try:
        await post_creator_video(request.app["bot"], video)
    except ChannelUnavailable:
        forget_video(video.video_id)
        return web.Response(status=503, text="Channel unavailable")
    except Exception:
        forget_video(video.video_id)
        raise

    return web.Response(status=200, text="OK")


async def _creator_inactive_handler(request: web.Request) -> web.Response:
    if not _video_secret_ok(request):
        return web.Response(status=401, text="Unauthorized")

    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    if not isinstance(payload, dict):
        return web.Response(status=400, text="Invalid payload")

    try:
        report = parse_inactive_report(payload)
    except PayloadError as exc:
        return web.Response(status=400, text=str(exc))

    try:
        await post_inactive_creators(request.app["bot"], report)
    except ChannelUnavailable:
        return web.Response(status=503, text="Channel unavailable")

    return web.Response(status=200, text="OK")


async def _health_handler(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


def create_webhook_app(bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/webhooks/creator-signup", _creator_signup_handler)
    app.router.add_post("/webhooks/creator-application", _creator_application_handler)
    app.router.add_post("/webhooks/creator-video", _creator_video_handler)
    app.router.add_post("/webhooks/creator-inactive", _creator_inactive_handler)
    app.router.add_get("/health", _health_handler)

    from carousel_hosting import register_routes

    register_routes(app)
    return app


async def start_creator_signup_webhook(bot) -> web.AppRunner | None:
    port = webhook_port()
    if port is None:
        return None

    app = create_webhook_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(
        "Creator webhooks listening on "
        f":{port}/webhooks/creator-signup, /webhooks/creator-application, "
        "/webhooks/creator-video and /webhooks/creator-inactive"
    )
    return runner
