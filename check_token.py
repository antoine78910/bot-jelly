"""Verify DISCORD_TOKEN before running the bot."""
import asyncio
import os
import sys

import discord
from dotenv import load_dotenv

load_dotenv(override=True)


def _clean_token() -> str:
    return os.getenv("DISCORD_TOKEN", "").strip().strip('"').strip("'")


def validate_token_format(token: str) -> list[str]:
    issues: list[str] = []
    if not token:
        issues.append("DISCORD_TOKEN is empty in .env")
    if " " in token or "\n" in token or "\r" in token:
        issues.append("Token contains spaces or line breaks — recopy it on one line")
    if token.count(".") != 2:
        issues.append(f"Token should contain exactly 2 dots (found {token.count('.')})")
    if len(token) < 50:
        issues.append(f"Token looks too short ({len(token)} chars)")
    if token.isdigit():
        issues.append("This looks like an application ID, not a bot token")
    return issues


async def test_login(token: str) -> None:
    client = discord.Client(intents=discord.Intents.default())

    @client.event
    async def on_ready():
        print(f"OK — connected as {client.user} (id {client.user.id})")
        await client.close()

    try:
        await client.start(token)
    except discord.LoginFailure:
        print("FAIL - Discord rejected the token (401 Unauthorized)")
        print("Reset: discord.com/developers -> your app -> Bot -> Reset Token")
        print("Paste the new token in .env as DISCORD_TOKEN=... (no quotes)")
        sys.exit(1)


if __name__ == "__main__":
    token = _clean_token()
    issues = validate_token_format(token)
    if issues:
        print("Token format problems:")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)

    asyncio.run(test_login(token))
