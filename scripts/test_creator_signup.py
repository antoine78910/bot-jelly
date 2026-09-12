"""Send a test creator signup notification to the Creators Discord channel."""

from __future__ import annotations

import asyncio
import os
import sys

import discord
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from creator_signups import CreatorSignup, log_creator_signup  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"), override=True)


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip().strip('"').strip("'")
    if not token:
        raise SystemExit("DISCORD_TOKEN missing in .env")

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        signup = CreatorSignup(
            email="marie.dupont@example.com",
            country="France",
            accounts=(
                "• **Instagram:** @marie.jobsearch\n"
                "• **TikTok:** @marie.career.tips"
            ),
            user_id="test-user-001",
            full_name="Marie Dupont",
            signed_up_at="2026-09-12T08:22:00Z",
        )

        message = await log_creator_signup(client, signup)
        if message is None:
            print("Failed to send test notification (channel not found?)")
        else:
            print(f"Test notification sent: {message.jump_url}")

        await client.close()

    await client.start(token)


if __name__ == "__main__":
    asyncio.run(main())
