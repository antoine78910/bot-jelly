"""One-shot: force-republish account_setup channel."""
from __future__ import annotations

import asyncio
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import discord
from dotenv import load_dotenv

from publisher import publish_channel

load_dotenv(override=True)

TOKEN = os.getenv("DISCORD_TOKEN", "").strip().strip('"').strip("'")
CHANNEL_ID = 1546460016184266836


async def main() -> None:
    intents = discord.Intents.default()
    intents.guilds = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        channel = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
        ok = await publish_channel(channel, "account_setup", client.user, force=True)
        print("published" if ok else "no change", getattr(channel, "name", CHANNEL_ID))
        await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
