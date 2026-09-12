"""Optional HTTP webhook for Supabase / JobShift creator signup events."""

from __future__ import annotations

import os

from aiohttp import web

from creator_signups import handle_signup_webhook, verify_webhook_secret


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


async def start_creator_signup_webhook(bot) -> web.AppRunner | None:
    port = webhook_port()
    if port is None:
        return None

    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/webhooks/creator-signup", _creator_signup_handler)
    app.router.add_get("/health", lambda _: web.Response(text="OK"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Creator signup webhook listening on :{port}/webhooks/creator-signup")
    return runner
