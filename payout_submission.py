import json
import re
import uuid
from pathlib import Path

import discord

from channel_utils import delete_bot_messages
from embeds import EMBED_COLOR
from notify_roles import notify_role_id, notify_role_mention

PAYOUT_SUBMISSION_TEMPLATE = "payout_submission_welcome"
CONFIG_PATH = Path(__file__).parent / "channel_config.json"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _staff_role_ids() -> list[int]:
    ids: list[int] = []
    for raw in _load_config().get("staff_role_ids", []):
        if str(raw).isdigit():
            ids.append(int(raw))
    return ids


def _ticket_category_id() -> int | None:
    raw = _load_config().get("payout_ticket_category_id")
    if raw and str(raw).isdigit():
        return int(raw)
    return None


def submission_embed() -> discord.Embed:
    return discord.Embed(
        title="💰 Demande de paiement",
        description=(
            "Envoie tes stats de compte en cliquant sur le bouton ci-dessous.\n\n"
            "Un salon ticket privé s’ouvre entre toi et les admins. "
            "Dépose-y tes vues, tes liens de posts et tes captures — "
            "les paiements sont traités sous 48 heures."
        ),
        color=EMBED_COLOR,
    )


def ticket_embed(user: discord.Member | discord.User, ticket_id: str) -> discord.Embed:
    embed = discord.Embed(
        title="💸 Demande de paiement",
        description=(
            f"Salut {user.mention} — voici ton ticket de paiement.\n\n"
            "**Réponds dans ce salon avec :**\n"
            "1. Ton pseudo Instagram (compte posting)\n"
            "2. La période couverte (ex. 1–15 mai 2026)\n"
            "3. Le total de vues sur tous tes posts\n"
            "4. Un enregistrement d’écran de tes analytics\n\n"
            "On vérifie et on traite sous **48 heures**. "
            "Tu recevras un DM dès que le paiement est envoyé."
        ),
        color=EMBED_COLOR,
    )
    embed.set_footer(text=f"Ticket ID: {ticket_id}")
    return embed


def _sanitize_username(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]", "", name.lower())
    return (cleaned or "user")[:32]


def _ticket_channel_name(user: discord.User) -> str:
    return f"payout-{_sanitize_username(user.name)}"


def _can_use_ticket_actions(member: discord.Member, channel: discord.TextChannel) -> bool:
    """Anyone who can see the ticket channel may mark paid or close it."""
    return channel.permissions_for(member).view_channel


async def _ticket_overwrites(
    guild: discord.Guild,
    user: discord.Member,
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            attach_files=True,
            read_message_history=True,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
        ),
    }
    for role_id in _staff_role_ids():
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
            )

    notify_id = notify_role_id()
    if notify_id and notify_id not in _staff_role_ids():
        notify_role = guild.get_role(notify_id)
        if notify_role:
            overwrites[notify_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
            )
    return overwrites


def _find_open_ticket(guild: discord.Guild, user: discord.User) -> discord.TextChannel | None:
    name = _ticket_channel_name(user)
    channel = discord.utils.get(guild.text_channels, name=name)
    if channel is None:
        return None
    perms = channel.permissions_for(user)
    if perms.view_channel:
        return channel
    return None


async def _create_ticket_channel(
    interaction: discord.Interaction,
) -> discord.TextChannel:
    guild = interaction.guild
    assert guild is not None
    member = interaction.user
    assert isinstance(member, discord.Member)

    submission = interaction.channel
    category = None
    cat_id = _ticket_category_id()
    if cat_id:
        category = guild.get_channel(cat_id)
    elif isinstance(submission, discord.TextChannel) and submission.category:
        category = submission.category

    return await guild.create_text_channel(
        name=_ticket_channel_name(member),
        category=category if isinstance(category, discord.CategoryChannel) else None,
        overwrites=await _ticket_overwrites(guild, member),
        reason=f"Payout ticket for {member}",
    )


class PayoutTicketView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Marquer payé et fermer",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="jelly_payout_mark_paid",
    )
    async def mark_paid(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return

        if not isinstance(interaction.user, discord.Member) or not _can_use_ticket_actions(
            interaction.user, channel
        ):
            await interaction.response.send_message(
                "Tu n’as pas la permission de gérer ce ticket.",
                ephemeral=True,
            )
            return

        opener = _ticket_opener(channel)
        if opener:
            try:
                await opener.send(
                    "✅ Ton paiement a été traité et envoyé. Merci de poster avec **JobShift** !"
                )
            except discord.HTTPException:
                pass

        await interaction.response.send_message("Ticket marqué comme payé. Fermeture…", ephemeral=True)
        await channel.delete(reason=f"Paid & closed by {interaction.user}")

    @discord.ui.button(
        label="Fermer le ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="jelly_payout_close",
    )
    async def close_ticket(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return

        if not isinstance(interaction.user, discord.Member) or not _can_use_ticket_actions(
            interaction.user, channel
        ):
            await interaction.response.send_message(
                "Tu n’as pas la permission de gérer ce ticket.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message("Fermeture du ticket…", ephemeral=True)
        await channel.delete(reason=f"Closed by {interaction.user}")


def _ticket_opener(channel: discord.TextChannel) -> discord.User | None:
    for target, overwrite in channel.overwrites.items():
        if isinstance(target, discord.Member) and not target.bot:
            if overwrite.view_channel is True:
                return target
    return None


class PayoutSubmitView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Demander un paiement",
        style=discord.ButtonStyle.success,
        emoji="💸",
        custom_id="jelly_payout_submit",
    )
    async def submit_payout(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Ce bouton fonctionne uniquement sur un serveur.",
                ephemeral=True,
            )
            return

        existing = _find_open_ticket(interaction.guild, interaction.user)
        if existing:
            await interaction.response.send_message(
                f"✅ Ton ticket de paiement est prêt : {existing.mention}",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            ticket_channel = await _create_ticket_channel(interaction)
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Impossible de créer ton ticket : {exc}",
                ephemeral=True,
            )
            return

        ticket_id = str(uuid.uuid4())
        ping = notify_role_mention()
        await ticket_channel.send(
            content=ping or None,
            embed=ticket_embed(interaction.user, ticket_id),
            view=PayoutTicketView(),
        )

        await interaction.followup.send(
            f"✅ Ton ticket de paiement est prêt : {ticket_channel.mention}",
            ephemeral=True,
        )


def payout_submission_panel_fingerprint() -> str:
    from publish_sync import _embed_payload, _hash_payload

    return _hash_payload(
        {
            "embed": _embed_payload(submission_embed()),
            "buttons": ["jelly_payout_submit"],
        }
    )


async def publish_payout_submission_welcome(
    channel: discord.TextChannel,
    bot_user: discord.ClientUser,
    *,
    force: bool = False,
) -> list[int]:
    if force:
        await delete_bot_messages(channel, bot_user)
        sent = await channel.send(embed=submission_embed(), view=PayoutSubmitView())
        return [sent.id]

    from publish_sync import sync_embed_messages

    return await sync_embed_messages(
        channel, bot_user, [submission_embed()], view=PayoutSubmitView()
    )
