import asyncio
import os
import sys

import discord
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"), override=True)

GUILD_ID = 1540823867818385468
NEEDLE = (sys.argv[1] if len(sys.argv) > 1 else "antoine2361").lower()


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip().strip('"').strip("'")
    intents = discord.Intents.default()
    intents.members = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        guild = client.get_guild(GUILD_ID)
        print(f"guild={guild}")
        if guild is not None:
            await guild.chunk()
            for member in guild.members:
                hay = " ".join(
                    filter(
                        None,
                        [
                            member.name,
                            member.display_name,
                            member.global_name,
                            str(member.id),
                        ],
                    )
                ).lower()
                if NEEDLE in hay:
                    print(
                        f"MATCH id={member.id} name={member.name} "
                        f"display={member.display_name} global={member.global_name}"
                    )
        await client.close()

    await client.start(token)


if __name__ == "__main__":
    asyncio.run(main())
