"""Approved TikTok sounds panel for clippers."""

from __future__ import annotations

import discord

from channel_utils import delete_bot_messages

MUSIC_TEMPLATE = "music"
MUSIC_COLOR = 0x9B59B6

MAIN_SOUND = {
    "label": "Original sound",
    "url": "https://vm.tiktok.com/ZN9SDDs4nx3UM-zamrh/",
}

ALT_SOUNDS = [
    {
        "label": "Bunna Summa",
        "url": "https://vm.tiktok.com/ZN9SDDoJbgHFd-82OiW/",
    },
    {
        "label": "Original sound",
        "url": "https://vt.tiktok.com/ZS9SDU6CdLt7Q-e6blR/",
    },
    {
        "label": "Volt Slope",
        "url": "https://vt.tiktok.com/ZS9SDUfGPkmJr-SbyoI/",
    },
    {
        "label": "Original sound",
        "url": "https://vt.tiktok.com/ZS9SDUuM46t44-F2LS1/",
    },
    {
        "label": "I Need a Dollar (Instrumental)",
        "url": "https://vt.tiktok.com/ZS9SDUQKyXvCd-tg0qR/",
    },
]


def panel_embed() -> discord.Embed:
    alt_lines = "\n".join(
        f"{index}. **{item['label']}**\n{item['url']}"
        for index, item in enumerate(ALT_SOUNDS, start=1)
    )
    return discord.Embed(
        title="🎵 Sounds to use",
        description=(
            "Use these TikTok sounds on your videos.\n\n"
            "**Main sound — at least 3 out of 4 videos**\n"
            f"**{MAIN_SOUND['label']}**\n"
            f"{MAIN_SOUND['url']}\n\n"
            "This is the default. Only switch when you need a change.\n\n"
            "**Alternatives — rotate the remaining video**\n"
            f"{alt_lines}\n\n"
            "Tap the buttons below to open the sound in TikTok."
        ),
        color=MUSIC_COLOR,
    )


class MusicLinksView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Main sound (use 3/4)",
                style=discord.ButtonStyle.link,
                url=MAIN_SOUND["url"],
                emoji="⭐",
                row=0,
            )
        )
        for index, item in enumerate(ALT_SOUNDS, start=1):
            self.add_item(
                discord.ui.Button(
                    label=f"Alt {index} · {item['label']}"[:80],
                    style=discord.ButtonStyle.link,
                    url=item["url"],
                    row=1,
                )
            )


def music_panel_fingerprint() -> str:
    from publish_sync import _embed_payload, _hash_payload

    return _hash_payload(
        {
            "embed": _embed_payload(panel_embed()),
            "main": MAIN_SOUND,
            "alts": ALT_SOUNDS,
        }
    )


async def publish_music_panel(
    channel: discord.TextChannel,
    bot_user: discord.ClientUser,
    *,
    force: bool = False,
) -> list[int]:
    if force:
        await delete_bot_messages(channel, bot_user)
        sent = await channel.send(embed=panel_embed(), view=MusicLinksView())
        return [sent.id]

    from publish_sync import sync_embed_messages

    return await sync_embed_messages(
        channel, bot_user, [panel_embed()], view=MusicLinksView()
    )
