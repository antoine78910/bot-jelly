"""List text channels the bot can access (find content-bot ID)."""
import asyncio
import os

import discord
from dotenv import load_dotenv

load_dotenv(override=True)
TOKEN = os.getenv("DISCORD_TOKEN", "").strip().strip('"').strip("'")


async def main() -> None:
    intents = discord.Intents.default()
    intents.guilds = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        for guild in client.guilds:
            print(f"\nGuild: {guild.name} ({guild.id})")
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                name = ch.name.encode("ascii", "replace").decode("ascii")
                print(f"  {ch.id}  #{name}")
        await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
