import discord


async def delete_bot_messages(channel: discord.TextChannel, bot_user: discord.ClientUser) -> int:
    """Supprime les anciens messages du bot dans ce salon."""
    deleted = 0
    async for message in channel.history(limit=100):
        if message.author.id != bot_user.id:
            continue
        try:
            await message.delete()
            deleted += 1
        except discord.HTTPException:
            pass
    return deleted
