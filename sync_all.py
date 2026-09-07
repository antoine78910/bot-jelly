"""Force-sync all channels from channel_config.json (local CLI)."""
import asyncio
import os
import sys
import traceback

# Windows console: avoid UnicodeEncodeError on channel names with emojis
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import discord
from dotenv import load_dotenv

from publisher import load_channel_config, publish_channel

load_dotenv(override=True)

TOKEN = os.getenv("DISCORD_TOKEN", "").strip().strip('"').strip("'")


async def main() -> None:
    force = "force" in sys.argv
    mapping = load_channel_config()
    if not mapping:
        print("No channels in channel_config.json")
        return

    intents = discord.Intents.default()
    intents.guilds = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"Logged in as {client.user}")
        updated = skipped = failed = 0

        for channel_id, template_name in mapping.items():
            label = f"{template_name} -> {channel_id}"
            try:
                channel = client.get_channel(int(channel_id))
                if channel is None:
                    channel = await client.fetch_channel(int(channel_id))
                if not isinstance(channel, discord.TextChannel):
                    print(f"FAIL {label}: not a text channel ({type(channel)})")
                    failed += 1
                    continue

                ok = await publish_channel(channel, template_name, client.user, force=force)
                ch_label = getattr(channel, "name", None) or str(channel.id)
                if ok:
                    print(f"OK   {label} (channel {channel.id} / {ch_label})")
                    updated += 1
                else:
                    print(f"SKIP {label} (channel {channel.id} / {ch_label})")
                    skipped += 1
            except Exception:
                print(f"FAIL {label}:")
                traceback.print_exc()
                failed += 1

        print(f"\nDone: {updated} updated, {skipped} skipped, {failed} failed")
        await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN missing in .env")
    asyncio.run(main())
