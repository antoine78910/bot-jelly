import json
import os
from pathlib import Path

import discord

from channel_utils import delete_bot_messages
from embeds import EMBED_COLOR
from notify_roles import notify_role_mention

REGISTRATION_TEMPLATE = "registration_welcome"
DEFAULT_LOG_CHANNEL_ID = 1509831488244547625

CONFIG_PATH = Path(__file__).parent / "channel_config.json"


def _log_channel_id() -> int:
    env = os.getenv("REGISTRATION_LOG_CHANNEL_ID", "").strip()
    if env.isdigit():
        return int(env)

    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        cfg = data.get("registration_log_channel")
        if cfg and str(cfg).isdigit():
            return int(cfg)

    return DEFAULT_LOG_CHANNEL_ID


def welcome_embed() -> discord.Embed:
    return discord.Embed(
        title="🚀 Welcome to the Jelly Clipping Campaign",
        description=(
            "Click the button below to begin your registration.\n\n"
            "**You'll be asked for:**\n"
            "• Instagram username\n"
            "• Phone Number\n"
            "• Payout method (PayPal / Crypto / Bank Transfer)\n"
            "• Payout details\n\n"
            "Once you register, you're officially in the campaign — "
            "start clipping, posting, and earning right away."
        ),
        color=EMBED_COLOR,
    )


class RegistrationModal(discord.ui.Modal, title="Jelly Clipping Registration"):
    instagram = discord.ui.TextInput(
        label="Instagram Username (Clipping Account)",
        placeholder="@yourhandle",
        required=True,
        max_length=100,
    )
    phone = discord.ui.TextInput(
        label="Phone Number",
        placeholder="+1 555 555 5555",
        required=True,
        max_length=30,
    )
    payout_method = discord.ui.TextInput(
        label="Payout Method",
        placeholder="PayPal / Crypto / Bank Transfer",
        required=True,
        max_length=100,
    )
    payout_details = discord.ui.TextInput(
        label="Payout Details (email / wallet / bank)",
        placeholder="your@email.com / wallet address / IBAN...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="New register",
            description=f"**Discord:** {interaction.user.mention} (`{interaction.user.id}`)",
            color=EMBED_COLOR,
        )
        embed.add_field(
            name="Instagram Username",
            value=self.instagram.value or "—",
            inline=False,
        )
        embed.add_field(name="Phone Number", value=self.phone.value or "—", inline=False)
        embed.add_field(name="Payout Method", value=self.payout_method.value or "—", inline=False)
        embed.add_field(
            name="Payout Details",
            value=self.payout_details.value or "—",
            inline=False,
        )
        embed.set_footer(text="Jelly Clipping Registration")
        embed.timestamp = discord.utils.utcnow()

        log_channel = interaction.client.get_channel(_log_channel_id()) if interaction.client else None
        if isinstance(log_channel, discord.TextChannel):
            ping = notify_role_mention()
            await log_channel.send(content=ping or None, embed=embed)

        confirm = discord.Embed(
            title="✅ Registration complete",
            description="You're officially in the **Jelly** Clipping campaign.",
            color=EMBED_COLOR,
        )
        await interaction.response.send_message(embed=confirm, ephemeral=True)


class RegisterView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Register",
        style=discord.ButtonStyle.primary,
        custom_id="jelly_clipping_register",
    )
    async def register(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(RegistrationModal())


def registration_panel_fingerprint() -> str:
    from publish_sync import _embed_payload, _hash_payload

    return _hash_payload(
        {
            "embed": _embed_payload(welcome_embed()),
            "buttons": ["jelly_clipping_register"],
        }
    )


async def publish_registration_welcome(
    channel: discord.TextChannel,
    bot_user: discord.ClientUser,
    *,
    force: bool = False,
) -> list[int]:
    if force:
        await delete_bot_messages(channel, bot_user)
        sent = await channel.send(embed=welcome_embed(), view=RegisterView())
        return [sent.id]

    from publish_sync import sync_embed_messages

    return await sync_embed_messages(
        channel, bot_user, [welcome_embed()], view=RegisterView()
    )
