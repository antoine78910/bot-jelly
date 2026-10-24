import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands

from channel_utils import delete_bot_messages
from countries import (
    ELIGIBLE_COUNTRY_CODES,
    country_label,
    flag,
    is_eligible,
    other_country_buckets,
)
from embeds import EMBED_COLOR
from notify_roles import notify_mentions
from staff import is_staff as _is_staff

REGISTRATION_TEMPLATE = "registration_welcome"
DEFAULT_LOG_CHANNEL_ID = 0
DEFAULT_CREATOR_ROLE_ID = 1562457257093103716
ROLE_SYNC_INTERVAL_SECONDS = 15 * 60
HISTORY_SCAN_LIMIT = 1000

CONFIG_PATH = Path(__file__).parent / "channel_config.json"

APPROVED_COLOR = 0x22C55E
REJECTED_COLOR = 0xEF4444
REPLACED_COLOR = 0x6B7280

TITLE_PENDING = "Nouvelle inscription"
TITLE_APPROVED = "Inscription — validée"
TITLE_REJECTED = "Inscription — refusée"
TITLE_REPLACED = "Inscription — remplacée"
TITLE_INELIGIBLE = "Inscription — pays non éligible"
FOOTER = "Inscription JobShift Posting"

FIELD_DISCORD = "Pseudo Discord"
FIELD_COUNTRY = "Pays"
FIELD_INSTAGRAM = "Pseudo Instagram"
FIELD_TIKTOK = "Pseudo TikTok"
FIELD_PHONE = "Téléphone"
FIELD_METHOD = "Méthode de paiement"
FIELD_DETAILS = "Infos de paiement"
FIELD_STATUS = "Statut"
FIELD_VALIDATED_BY = "Validé par"

STATUS_LABELS = {
    "pending": "En attente de validation",
    "approved": "Validée",
    "rejected": "Refusée",
    "ineligible": "Refus automatique : pays non éligible (bloqué)",
}

NOT_YET = "Pas encore (à configurer plus tard)"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _config_int(config_key: str, env_key: str, default: int) -> int:
    env = os.getenv(env_key, "").strip()
    if env.isdigit():
        return int(env)
    raw = _load_config().get(config_key)
    if raw and str(raw).isdigit():
        return int(raw)
    return default


def _log_channel_id() -> int:
    return _config_int("registration_log_channel", "REGISTRATION_LOG_CHANNEL_ID", DEFAULT_LOG_CHANNEL_ID)


def creator_role_id() -> int:
    return _config_int("creator_role_id", "CREATOR_ROLE_ID", DEFAULT_CREATOR_ROLE_ID)


def _guild_id() -> int:
    return _config_int("guild_id", "GUILD_ID", 0)


# ---------------------------------------------------------------------------
# Store (JSON). The registration log channel is the source of truth: the store
# is rebuilt from it on startup, so a wiped container loses nothing.
# ---------------------------------------------------------------------------


def _store_path() -> Path:
    env = os.getenv("CREATOR_STORE_PATH", "").strip()
    if env:
        return Path(env)
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume:
        return Path(volume) / "creator-registrations.json"
    return Path(__file__).parent / "data" / "creator-registrations.json"


_registrations: dict[str, dict] = {}
_store_loaded = False


def _load_store() -> None:
    global _store_loaded, _registrations
    if _store_loaded:
        return
    _store_loaded = True
    path = _store_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw.get("registrations") or []
        _registrations = {str(r["discord_id"]): r for r in rows if r.get("discord_id")}
        print(f"[CREATOR] Loaded {len(_registrations)} registration(s) from {path}")
    except FileNotFoundError:
        _registrations = {}
    except (OSError, ValueError) as exc:
        print(f"[CREATOR] Could not load store, starting fresh: {exc}")
        _registrations = {}


def _save_store() -> None:
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"registrations": list(_registrations.values())}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"[CREATOR] FAILED to save store {path}: {exc}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(value: str | None, limit: int = 1000) -> str:
    text = str(value or "").strip()
    if not text or text == "—":
        return "—"
    return text[:limit]


def _handle(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower().lstrip("@"))


# ---------------------------------------------------------------------------
# Embeds
# ---------------------------------------------------------------------------


def welcome_embed() -> discord.Embed:
    return discord.Embed(
        title="🚀 Bienvenue dans la campagne JobShift Posting",
        description=(
            "Clique sur le bouton ci-dessous pour t’inscrire.\n"
            "🌍 Campagne ouverte pour l’instant en **France**, **Belgique** et **Suisse**.\n\n"
            "**On te demandera :**\n"
            "• Ton pays\n"
            "• Tes pseudos Instagram et TikTok (ou « pas encore », à configurer plus tard)\n"
            "• Ton numéro de téléphone\n"
            "• Ta méthode et tes infos de paiement (PayPal / Crypto / Virement)\n\n"
            "Une fois ta demande acceptée, tu reçois le rôle **Creator** "
            "et tu peux commencer à créer ton contenu."
        ),
        color=EMBED_COLOR,
    )


def _country_text(code: str | None) -> str:
    if not code:
        return "—"
    return f"{flag(code)} {country_label(code)} ({code})"


def _country_code_from_text(text: str) -> str:
    match = re.search(r"\(([A-Z]{2})\)", text or "")
    return match.group(1) if match else ""


def registration_embed(record: dict) -> discord.Embed:
    status = record.get("status") or "pending"
    color = {"approved": APPROVED_COLOR, "rejected": REJECTED_COLOR, "ineligible": REJECTED_COLOR}.get(
        status, EMBED_COLOR
    )
    title = {
        "approved": TITLE_APPROVED,
        "rejected": TITLE_REJECTED,
        "ineligible": TITLE_INELIGIBLE,
    }.get(status, TITLE_PENDING)
    username = record.get("discord_username") or ""
    who = f" · @{username}" if username else ""
    embed = discord.Embed(
        title=title,
        description=f"**Discord :** <@{record['discord_id']}> (`{record['discord_id']}`){who}",
        color=color,
    )
    embed.add_field(name=FIELD_DISCORD, value=_clip(f"@{username}" if username else "", 100), inline=False)
    embed.add_field(name=FIELD_COUNTRY, value=_country_text(record.get("country")), inline=False)
    embed.add_field(name=FIELD_INSTAGRAM, value=_clip(record.get("instagram"), 100), inline=False)
    embed.add_field(name=FIELD_TIKTOK, value=_clip(record.get("tiktok"), 100), inline=False)
    embed.add_field(name=FIELD_PHONE, value=_clip(record.get("phone"), 40), inline=False)
    embed.add_field(name=FIELD_METHOD, value=_clip(record.get("payout_method"), 100), inline=False)
    embed.add_field(name=FIELD_DETAILS, value=_clip(record.get("payout_details")), inline=False)
    embed.add_field(name=FIELD_STATUS, value=STATUS_LABELS.get(status, STATUS_LABELS["pending"]), inline=False)
    validator = record.get("validated_by") or {}
    if validator.get("id"):
        embed.add_field(
            name=FIELD_VALIDATED_BY,
            value=_clip(f"@{validator.get('username') or 'staff'} (`{validator['id']}`)", 200),
            inline=False,
        )
    embed.set_footer(text=FOOTER)
    applied = record.get("applied_at")
    try:
        embed.timestamp = datetime.fromisoformat(applied) if applied else discord.utils.utcnow()
    except ValueError:
        embed.timestamp = discord.utils.utcnow()
    return embed


def _replaced_embed(record: dict) -> discord.Embed:
    embed = registration_embed({**record, "status": "pending", "validated_by": None})
    embed.title = TITLE_REPLACED
    embed.color = REPLACED_COLOR
    for index, field in enumerate(embed.fields):
        if field.name == FIELD_STATUS:
            embed.set_field_at(index, name=FIELD_STATUS, value="Remplacée par une inscription plus récente", inline=False)
    return embed


def _lookup_embed(record: dict) -> discord.Embed:
    status = record.get("status") or "pending"
    color = {"approved": APPROVED_COLOR, "rejected": REJECTED_COLOR, "ineligible": REJECTED_COLOR}.get(
        status, EMBED_COLOR
    )
    embed = discord.Embed(
        title=f"@{record.get('discord_username') or record['discord_id']}",
        description=(
            f"**Statut :** {STATUS_LABELS.get(status, STATUS_LABELS['pending'])}\n"
            f"**Discord :** <@{record['discord_id']}> (`{record['discord_id']}`)\n"
            f"**Pays :** {_country_text(record.get('country'))}"
        ),
        color=color,
    )
    embed.add_field(name="Instagram", value=_clip(record.get("instagram"), 100), inline=True)
    embed.add_field(name="TikTok", value=_clip(record.get("tiktok"), 100), inline=True)
    embed.add_field(name="Téléphone", value=_clip(record.get("phone"), 40), inline=True)
    embed.add_field(name="Méthode de paiement", value=_clip(record.get("payout_method"), 100), inline=False)
    embed.add_field(name="Infos de paiement", value=_clip(record.get("payout_details")), inline=False)
    validator = record.get("validated_by") or {}
    review = [
        f"Inscrit le {record['applied_at']}" if record.get("applied_at") else "",
        f"Mis à jour le {record['validated_at']}" if record.get("validated_at") else "",
        f"Par @{validator['username']}" if validator.get("username") else "",
    ]
    embed.add_field(name="Suivi", value=_clip("\n".join(line for line in review if line), 300), inline=False)
    embed.set_footer(text="Enregistré pour les futurs paiements")
    return embed


def _record_from_message(message: discord.Message) -> dict | None:
    """Rebuild a registration from a bot log message (new and legacy formats)."""
    if not message.embeds:
        return None
    embed = message.embeds[0]
    title = embed.title or ""
    if not re.search(r"inscription", title, re.I) or re.search(r"remplac", title, re.I):
        return None
    fields = {field.name: str(field.value or "") for field in embed.fields}
    if FIELD_INSTAGRAM not in fields:
        return None
    description = embed.description or ""
    match = re.search(r"<@!?(\d+)>", description)
    if not match:
        return None
    discord_id = match.group(1)
    username = (re.search(r"· @(\S+)", description) or [None, ""])[1]
    username = fields.get(FIELD_DISCORD, "").lstrip("@").split(" ")[0].strip("—") or username

    status_text = f"{fields.get(FIELD_STATUS, '')} {title}"
    status = "pending"
    if re.search(r"non éligible", status_text, re.I):
        status = "ineligible"
    elif re.search(r"validée", status_text, re.I):
        status = "approved"
    elif re.search(r"refusée", status_text, re.I):
        status = "rejected"

    validator = fields.get(FIELD_VALIDATED_BY, "")
    validator_id = (re.search(r"`(\d+)`", validator) or [None, ""])[1]
    validator_name = (re.search(r"@(\S+)", validator) or [None, ""])[1]
    return {
        "discord_id": discord_id,
        "discord_username": username,
        "country": _country_code_from_text(fields.get(FIELD_COUNTRY, "")),
        "instagram": fields.get(FIELD_INSTAGRAM) or "—",
        "tiktok": fields.get(FIELD_TIKTOK) or "—",
        "phone": fields.get(FIELD_PHONE) or "—",
        "payout_method": fields.get(FIELD_METHOD) or "—",
        "payout_details": fields.get(FIELD_DETAILS) or "—",
        "status": status,
        "applied_at": message.created_at.isoformat(),
        "validated_at": None if status == "pending" else (message.edited_at or message.created_at).isoformat(),
        "validated_by": {"id": validator_id, "username": validator_name} if validator_id else None,
        "message_id": message.id,
        "channel_id": message.channel.id,
    }


# ---------------------------------------------------------------------------
# Creator role
# ---------------------------------------------------------------------------


async def _member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


async def grant_creator_role(guild: discord.Guild | None, user_id: int, reason: str) -> bool:
    role_id = creator_role_id()
    if not role_id or guild is None:
        return False
    member = await _member(guild, user_id)
    if member is None:
        return False
    if member.get_role(role_id):
        return True
    try:
        await member.add_roles(discord.Object(id=role_id), reason=reason)
        return True
    except discord.HTTPException as exc:
        print(f"[CREATOR] Could not grant creator role to {member}: {exc}")
        return False


async def backfill_creator_roles(guild: discord.Guild, *, quiet: bool = False) -> None:
    """Give the creator role to every approved applicant who is missing it."""
    role = guild.get_role(creator_role_id())
    if role is None:
        print(f"[CREATOR] Creator role {creator_role_id()} not found in {guild.name}.")
        return
    me = guild.me
    can_manage = bool(me and me.guild_permissions.manage_roles)
    above = bool(me and me.top_role > role)
    if not quiet:
        print(
            f'[CREATOR] Role check: "{role.name}" pos {role.position} · bot top role '
            f'"{me.top_role.name if me else "?"}" · Manage Roles {"yes" if can_manage else "NO"} · '
            f'bot above role {"yes" if above else "NO"}'
        )
    if not can_manage or not above:
        print("[CREATOR] Cannot grant the creator role: give the bot Manage Roles and move its role above Creator.")
        return

    counts = {"granted": 0, "already": 0, "left": 0, "failed": 0}
    for record in list(_registrations.values()):
        if record.get("status") != "approved":
            continue
        member = await _member(guild, int(record["discord_id"]))
        if member is None:
            counts["left"] += 1
            continue
        if member.get_role(role.id):
            counts["already"] += 1
            continue
        try:
            await member.add_roles(role, reason="Creator application approved")
            counts["granted"] += 1
        except discord.HTTPException as exc:
            counts["failed"] += 1
            print(f"[CREATOR] Could not grant creator role to {member}: {exc}")
    if not quiet or counts["granted"] or counts["failed"]:
        print(
            f"[CREATOR] Creator role backfill: {counts['granted']} granted, {counts['already']} already had it, "
            f"{counts['left']} not in server, {counts['failed']} failed"
        )


def _registration_guild(client: discord.Client) -> discord.Guild | None:
    channel = client.get_channel(_log_channel_id())
    if isinstance(channel, discord.TextChannel):
        return channel.guild
    return client.get_guild(_guild_id())


async def sync_registration_log(client: discord.Client) -> None:
    """Rebuild the store from the log channel, restore review buttons, backfill roles."""
    _load_store()
    channel = client.get_channel(_log_channel_id())
    if isinstance(channel, discord.TextChannel):
        latest: dict[str, discord.Message] = {}
        try:
            async for message in channel.history(limit=HISTORY_SCAN_LIMIT, oldest_first=True):
                if message.author.id != client.user.id:
                    continue
                parsed = _record_from_message(message)
                if parsed is None:
                    continue
                existing = _registrations.get(parsed["discord_id"], {})
                _registrations[parsed["discord_id"]] = {
                    **existing,
                    **parsed,
                    "discord_username": parsed["discord_username"] or existing.get("discord_username", ""),
                    "validated_by": parsed["validated_by"]
                    or (None if parsed["status"] == "pending" else existing.get("validated_by")),
                }
                latest[parsed["discord_id"]] = message
        except discord.HTTPException as exc:
            print(f"[CREATOR] Could not read registration log: {exc}")

        for discord_id, message in latest.items():
            record = _registrations.get(discord_id)
            if not record or record.get("status") != "pending" or record.get("message_id") != message.id:
                continue
            has_buttons = any(
                str(getattr(child, "custom_id", "") or "").startswith(f"{REVIEW_PREFIX}:")
                for row in message.components
                for child in getattr(row, "children", [])
            )
            if has_buttons:
                continue
            try:
                await message.edit(embed=registration_embed(record), view=_review_view(discord_id))
            except discord.HTTPException as exc:
                print(f"[CREATOR] Could not add review buttons: {exc}")
        _save_store()
    else:
        print("[CREATOR] Registration log channel not configured or not accessible.")

    print(f"[CREATOR] {len(_registrations)} registration(s) stored for /username")
    guild = _registration_guild(client)
    if guild is not None:
        await backfill_creator_roles(guild)


_role_sync_task: asyncio.Task | None = None


def start_creator_role_sync(client: discord.Client) -> None:
    """Re-run the backfill so creators who left and rejoined get the role back without a restart."""
    global _role_sync_task
    if _role_sync_task is not None and not _role_sync_task.done():
        return

    async def _loop() -> None:
        while True:
            await asyncio.sleep(ROLE_SYNC_INTERVAL_SECONDS)
            guild = _registration_guild(client)
            if guild is None:
                continue
            try:
                await backfill_creator_roles(guild, quiet=True)
            except Exception as exc:
                print(f"[CREATOR] Creator role sync failed: {exc}")

    _role_sync_task = asyncio.create_task(_loop())


# ---------------------------------------------------------------------------
# Staff review
# ---------------------------------------------------------------------------

REVIEW_PREFIX = "jobshift_reg"


class ReviewButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"jobshift_reg:(?P<action>approve|reject):(?P<user_id>\d+)",
):
    def __init__(self, action: str, user_id: int) -> None:
        approve = action == "approve"
        super().__init__(
            discord.ui.Button(
                label="Valider" if approve else "Refuser",
                emoji="✅" if approve else "✖️",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"{REVIEW_PREFIX}:{action}:{user_id}",
            )
        )
        self.action = action
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["action"], int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await review_registration(interaction, self.user_id, "approved" if self.action == "approve" else "rejected")


def _review_view(discord_id: str | int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ReviewButton("approve", int(discord_id)))
    view.add_item(ReviewButton("reject", int(discord_id)))
    return view


async def review_registration(interaction: discord.Interaction, user_id: int, status: str) -> None:
    if not _is_staff(interaction):
        await interaction.response.send_message(
            "Seul le staff peut valider les inscriptions.", ephemeral=True
        )
        return
    _load_store()
    discord_id = str(user_id)
    from_message = _record_from_message(interaction.message) if interaction.message else None
    existing = _registrations.get(discord_id)

    if existing and existing.get("message_id") and interaction.message and existing["message_id"] != interaction.message.id:
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            pass
        await interaction.response.send_message(
            "Cette notification a été remplacée par une inscription plus récente. Valide la dernière.",
            ephemeral=True,
        )
        return

    source = from_message or existing
    if not source or source.get("discord_id") != discord_id:
        await interaction.response.send_message(
            "Cette notification ne contient aucune inscription à valider.", ephemeral=True
        )
        return
    if (existing or {}).get("status") == status or source.get("status") == status:
        await interaction.response.send_message(
            "Déjà validée et enregistrée." if status == "approved" else "Déjà refusée.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    record = {
        **(existing or {}),
        **source,
        "discord_id": discord_id,
        "discord_username": (existing or {}).get("discord_username") or source.get("discord_username") or "",
        "status": status,
        "applied_at": (existing or {}).get("applied_at") or source.get("applied_at") or _now_iso(),
        "validated_at": _now_iso(),
        "validated_by": {"id": str(interaction.user.id), "username": interaction.user.name},
        "message_id": interaction.message.id if interaction.message else None,
        "channel_id": interaction.channel_id,
    }
    if not record["discord_username"]:
        try:
            user = await interaction.client.fetch_user(user_id)
            record["discord_username"] = user.name
        except discord.HTTPException:
            pass
    _registrations[discord_id] = record
    _save_store()

    if interaction.message:
        try:
            await interaction.message.edit(embed=registration_embed(record), view=None)
        except discord.HTTPException:
            pass
    role_granted = False
    if status == "approved":
        role_granted = await grant_creator_role(interaction.guild, user_id, "Creator application approved")

    if status == "approved":
        from embeds import channel_mention

        dm = (
            "✅ **Ta demande a été acceptée !** Tu es maintenant officiellement **créateur JobShift**.\n\n"
            f"Tu peux dès maintenant créer ton contenu : va dans {channel_mention('content_bot')} et clique sur "
            "**Générer du contenu**. La première fois, tu crées ton **persona** (ton personnage unique, "
            "à partir d’une photo), puis tes carrousels sont générés avec lui.\n\n"
            "Tes infos de paiement sont bien enregistrées."
        )
    else:
        dm = "Ton inscription à **JobShift Posting** n’a pas été retenue cette fois-ci."
    sent = False
    try:
        user = await interaction.client.fetch_user(user_id)
        await user.send(dm)
        sent = True
    except discord.HTTPException:
        pass

    no_dm = "" if sent else " Impossible de lui envoyer un DM."
    if status == "approved":
        lookup = record["discord_username"] or discord_id
        role_note = "" if role_granted else " ⚠️ Rôle Creator non attribué (permission Manage Roles du bot ?)."
        msg = (
            f"Validée et enregistrée pour les paiements. Retrouve-la avec `/username nom:{lookup}`."
            f"{role_note}{no_dm}"
        )
    else:
        msg = f"Refusée.{no_dm}"
    await interaction.followup.send(msg, ephemeral=True)


# ---------------------------------------------------------------------------
# Applicant flow
# ---------------------------------------------------------------------------


INELIGIBLE_MESSAGE = (
    "❌ **Tu n’es pas éligible pour l’instant.**\n"
    "La campagne **JobShift Posting** est ouverte uniquement en **France**, **Belgique** et **Suisse** "
    "pour le moment. Ta demande ne peut pas être refaite."
)


def _is_blocked(user_id: int) -> bool:
    _load_store()
    record = _registrations.get(str(user_id)) or {}
    return record.get("status") == "ineligible"


def _guess_payout_method(text: str) -> str:
    lowered = text.lower()
    if "paypal" in lowered:
        return "PayPal"
    if re.search(r"\b(crypto|usdt|usdc|btc|bitcoin|eth|ethereum|sol|solana|trc20|erc20|wallet)\b", lowered) or re.search(
        r"\b0x[0-9a-f]{20,}", lowered
    ):
        return "Crypto"
    if re.search(r"\b(iban|virement|rib|bic|swift|wise|revolut)\b", lowered) or re.search(
        r"\b[a-z]{2}\d{2}(?:\s?[a-z0-9]{4}){3,}", lowered
    ):
        return "Virement"
    if "@" in lowered:
        return "PayPal (email)"
    return "Voir infos de paiement"


async def _log_registration(client: discord.Client, record: dict, previous: dict | None, *, review: bool) -> None:
    """Post a registration (or automatic refusal) in the log channel and ping the owner."""
    log_channel = client.get_channel(_log_channel_id())
    if not isinstance(log_channel, discord.TextChannel):
        print("[CREATOR] Registration log channel not accessible, nothing posted.")
        return
    if previous and previous.get("message_id") and previous.get("status") == "pending":
        try:
            old = await log_channel.fetch_message(int(previous["message_id"]))
            await old.edit(embed=_replaced_embed(previous), view=None)
        except discord.HTTPException:
            pass
    try:
        kwargs = {"view": _review_view(record["discord_id"])} if review else {}
        sent = await log_channel.send(
            content=notify_mentions() or None,
            embed=registration_embed(record),
            **kwargs,
        )
        record["message_id"] = sent.id
        _save_store()
    except discord.HTTPException as exc:
        print(f"[CREATOR] Registration log failed: {exc}")


async def _refuse_ineligible(interaction: discord.Interaction, country: str) -> None:
    await interaction.response.edit_message(content=INELIGIBLE_MESSAGE, view=None)
    _load_store()
    discord_id = str(interaction.user.id)
    previous = _registrations.get(discord_id)
    if (previous or {}).get("status") == "ineligible":
        return
    record = {
        "discord_id": discord_id,
        "discord_username": interaction.user.name,
        "country": country,
        "instagram": "—",
        "tiktok": "—",
        "phone": "—",
        "payout_method": "—",
        "payout_details": "—",
        "status": "ineligible",
        "applied_at": _now_iso(),
        "validated_at": _now_iso(),
        "validated_by": None,
        "message_id": None,
        "channel_id": _log_channel_id() or None,
    }
    _registrations[discord_id] = record
    _save_store()
    print(f"[CREATOR] {interaction.user} refused: country {country} not eligible")
    await _log_registration(interaction.client, record, previous, review=False)


class _OwnedView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=900)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id


class CountryPickView(_OwnedView):
    PROMPT = (
        "🌍 **Dans quel pays habites-tu ?**\n"
        "Choisis ton pays ci-dessous (les autres pays sont classés par ordre alphabétique)."
    )

    def __init__(self, user_id: int) -> None:
        super().__init__(user_id)
        self.buckets = other_country_buckets()
        options = [
            discord.SelectOption(label=country_label(code), value=code, emoji=flag(code))
            for code in ELIGIBLE_COUNTRY_CODES
        ]
        options += [
            discord.SelectOption(label=f"Autre pays : {label}", value=f"bucket:{index}", emoji="🌍")
            for index, (label, _codes) in enumerate(self.buckets)
        ]
        self.main = discord.ui.Select(placeholder="Choisis ton pays…", options=options, row=0)
        self.main.callback = self._on_main
        self.add_item(self.main)
        self.detail: discord.ui.Select | None = None

    async def _on_main(self, interaction: discord.Interaction) -> None:
        value = self.main.values[0]
        if not value.startswith("bucket:"):
            if _is_blocked(interaction.user.id):
                await interaction.response.edit_message(content=INELIGIBLE_MESSAGE, view=None)
                return
            await interaction.response.send_modal(RegistrationModal(value))
            return
        label, codes = self.buckets[int(value.split(":", 1)[1])]
        if self.detail is not None:
            self.remove_item(self.detail)
        self.detail = discord.ui.Select(
            placeholder=f"Choisis ton pays ({label})…",
            options=[
                discord.SelectOption(label=country_label(code), value=code, emoji=flag(code)) for code in codes
            ],
            row=1,
        )
        self.detail.callback = self._on_detail
        self.add_item(self.detail)
        await interaction.response.edit_message(content=self.PROMPT, view=self)

    async def _on_detail(self, interaction: discord.Interaction) -> None:
        code = self.detail.values[0] if self.detail else ""
        if is_eligible(code):
            await interaction.response.send_modal(RegistrationModal(code))
            return
        await interaction.response.edit_message(
            content=(
                f"Tu as indiqué : **{flag(code)} {country_label(code)}**.\n\n"
                "⚠️ La campagne est ouverte uniquement en **France**, **Belgique** et **Suisse** pour l’instant. "
                "Si tu confirmes, ta demande sera refusée et **tu ne pourras pas la refaire**."
            ),
            view=ConfirmCountryView(self.user_id, code),
        )


class ConfirmCountryView(_OwnedView):
    def __init__(self, user_id: int, country: str) -> None:
        super().__init__(user_id)
        self.country = country

    @discord.ui.button(label="Confirmer mon pays", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _refuse_ineligible(interaction, self.country)

    @discord.ui.button(label="Changer de pays", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content=CountryPickView.PROMPT, view=CountryPickView(self.user_id)
        )


class RegistrationModal(discord.ui.Modal):
    def __init__(self, country: str) -> None:
        super().__init__(title="Inscription JobShift Posting", timeout=None)
        self.country = country
        self.instagram = discord.ui.TextInput(placeholder="@toncompte", required=False, max_length=100)
        self.tiktok = discord.ui.TextInput(placeholder="@toncompte", required=False, max_length=100)
        self.not_yet = discord.ui.CheckboxGroup(
            required=False,
            min_values=0,
            max_values=2,
            options=[
                discord.components.CheckboxGroupOption(label="Pas encore de compte Instagram", value="instagram"),
                discord.components.CheckboxGroupOption(label="Pas encore de compte TikTok", value="tiktok"),
            ],
        )
        self.phone = discord.ui.TextInput(placeholder="+33 6 12 34 56 78", required=True, max_length=30)
        self.payout = discord.ui.TextInput(
            placeholder="PayPal : ton@email.com / Crypto : USDT TRC20 + adresse / Virement : IBAN…",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
        )
        self.add_item(discord.ui.Label(text="Pseudo Instagram (compte posting)", component=self.instagram))
        self.add_item(discord.ui.Label(text="Pseudo TikTok (compte posting)", component=self.tiktok))
        self.add_item(
            discord.ui.Label(
                text="Pas encore de compte ?",
                description="Coche si tu n’as pas encore le compte : tu pourras le configurer plus tard.",
                component=self.not_yet,
            )
        )
        self.add_item(discord.ui.Label(text="Numéro de téléphone", component=self.phone))
        self.add_item(
            discord.ui.Label(
                text="Paiement : méthode + infos",
                description="PayPal, Crypto ou Virement, avec ton email / wallet / IBAN.",
                component=self.payout,
            )
        )

    @staticmethod
    def _account(value: str, not_yet: bool) -> str:
        handle = (value or "").strip()
        if not_yet or not handle:
            return NOT_YET
        return _clip(handle, 100)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Staff notification can exceed Discord's 3 s reply window.
        await interaction.response.defer(ephemeral=True, thinking=True)
        if _is_blocked(interaction.user.id):
            await interaction.followup.send(INELIGIBLE_MESSAGE, ephemeral=True)
            return
        not_yet = set(self.not_yet.values or [])
        payout = self.payout.value or ""
        record = {
            "discord_id": str(interaction.user.id),
            "discord_username": interaction.user.name,
            "country": self.country,
            "instagram": self._account(self.instagram.value, "instagram" in not_yet),
            "tiktok": self._account(self.tiktok.value, "tiktok" in not_yet),
            "phone": _clip(self.phone.value, 40),
            "payout_method": _guess_payout_method(payout),
            "payout_details": _clip(payout),
            "status": "pending",
            "applied_at": _now_iso(),
            "validated_at": None,
            "validated_by": None,
            "message_id": None,
            "channel_id": _log_channel_id() or None,
        }
        _load_store()
        previous = _registrations.get(record["discord_id"])
        _registrations[record["discord_id"]] = record
        _save_store()
        await _log_registration(interaction.client, record, previous, review=True)

        description = (
            "Ta demande pour la campagne **JobShift Posting** est bien envoyée.\n"
            "L’équipe la vérifie : dès qu’elle est acceptée, tu reçois ton rôle **Creator** "
            "et un DM pour commencer à créer ton contenu."
        )
        confirm = discord.Embed(title="✅ Demande envoyée", description=description, color=EMBED_COLOR)
        await interaction.followup.send(embed=confirm, ephemeral=True)


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
        if _is_blocked(interaction.user.id):
            await interaction.response.send_message(INELIGIBLE_MESSAGE, ephemeral=True)
            return
        await interaction.response.send_message(
            CountryPickView.PROMPT, view=CountryPickView(interaction.user.id), ephemeral=True
        )


# ---------------------------------------------------------------------------
# /username lookup (staff)
# ---------------------------------------------------------------------------


def find_registrations(query: str) -> list[dict]:
    _load_store()
    needle = _handle(query)
    if not needle:
        return []
    exact: list[dict] = []
    partial: list[dict] = []
    for record in _registrations.values():
        keys = [
            _handle(record.get(key))
            for key in ("discord_id", "discord_username", "instagram", "tiktok")
        ]
        keys = [key for key in keys if key and key != "—"]
        if needle in keys:
            exact.append(record)
        elif len(needle) >= 3 and any(needle in key for key in keys):
            partial.append(record)
    return exact or partial


@app_commands.command(
    name="username",
    description="Retrouve une inscription créateur et les infos de paiement enregistrées.",
)
@app_commands.describe(nom="Pseudo Discord, Instagram ou TikTok")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def username_command(interaction: discord.Interaction, nom: str) -> None:
    if not _is_staff(interaction):
        await interaction.response.send_message(
            "Seul le staff peut consulter les infos de paiement des créateurs.", ephemeral=True
        )
        return
    _load_store()
    if not _registrations:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await sync_registration_log(interaction.client)
        respond = interaction.followup.send
    else:
        respond = interaction.response.send_message
    matches = find_registrations(nom)[:5]
    if not matches:
        await respond(f"Aucune inscription trouvée pour `{nom}`.", ephemeral=True)
        return
    await respond(embeds=[_lookup_embed(record) for record in matches], ephemeral=True)


# ---------------------------------------------------------------------------
# Panel publishing
# ---------------------------------------------------------------------------


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
