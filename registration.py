import json
import os
from pathlib import Path

import discord

from channel_utils import delete_bot_messages
from embeds import EMBED_COLOR
from notify_roles import notify_role_mention

REGISTRATION_TEMPLATE = "registration_welcome"
DEFAULT_LOG_CHANNEL_ID = 0

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
        title="🚀 Bienvenue dans la campagne JobShift Posting",
        description=(
            "Clique sur le bouton ci-dessous pour t’inscrire.\n\n"
            "**On te demandera :**\n"
            "• Ton pseudo Instagram\n"
            "• Ton numéro de téléphone\n"
            "• Ta méthode de paiement (PayPal / Crypto / Virement)\n"
            "• Tes infos de paiement\n\n"
            "Une fois inscrit, tu es dans la campagne — "
            "tu peux poster et commencer à être payé."
        ),
        color=EMBED_COLOR,
    )


class RegistrationModal(discord.ui.Modal, title="Inscription JobShift Posting"):
    instagram = discord.ui.TextInput(
        label="Pseudo Instagram (compte posting)",
        placeholder="@toncompte",
        required=True,
        max_length=100,
    )
    phone = discord.ui.TextInput(
        label="Numéro de téléphone",
        placeholder="+33 6 12 34 56 78",
        required=True,
        max_length=30,
    )
    payout_method = discord.ui.TextInput(
        label="Méthode de paiement",
        placeholder="PayPal / Crypto / Virement",
        required=True,
        max_length=100,
    )
    payout_details = discord.ui.TextInput(
        label="Infos de paiement (email / wallet / IBAN)",
        placeholder="ton@email.com / adresse wallet / IBAN…",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Nouvelle inscription",
            description=f"**Discord :** {interaction.user.mention} (`{interaction.user.id}`)",
            color=EMBED_COLOR,
        )
        embed.add_field(
            name="Pseudo Instagram",
            value=self.instagram.value or "—",
            inline=False,
        )
        embed.add_field(name="Téléphone", value=self.phone.value or "—", inline=False)
        embed.add_field(name="Méthode de paiement", value=self.payout_method.value or "—", inline=False)
        embed.add_field(
            name="Infos de paiement",
            value=self.payout_details.value or "—",
            inline=False,
        )
        embed.set_footer(text="Inscription JobShift Posting")
        embed.timestamp = discord.utils.utcnow()

        log_channel = interaction.client.get_channel(_log_channel_id()) if interaction.client else None
        if isinstance(log_channel, discord.TextChannel):
            ping = notify_role_mention()
            await log_channel.send(content=ping or None, embed=embed)

        confirm = discord.Embed(
            title="✅ Inscription terminée",
            description="Tu es officiellement dans la campagne **JobShift Posting**.",
            color=EMBED_COLOR,
        )
        await interaction.response.send_message(embed=confirm, ephemeral=True)


class RegisterView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="S’inscrire",
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
