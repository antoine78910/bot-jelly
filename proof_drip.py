"""Drip-post payment proofs: one new screenshot every 2 days in the payment proof channel.

Images come from assets/payout_proofs (earliest transfer date first). The channel itself is
the state: a proof counts as posted when its file name or content hash is already attached
to a bot message there, and the countdown starts from the latest bot image. Restarts and
redeploys therefore never repost or reset anything.

Staff commands:
  /post-proof   post up to 10 uploaded proofs in order, or the next one from the folder
  /say          post a message and/or an image as the bot
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands

from payout_proofs import list_proof_image_paths
from staff import is_staff

CONFIG_PATH = Path(__file__).parent / "channel_config.json"
CHECK_EVERY_SECONDS = 60 * 60
HISTORY_LIMIT = 300
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
HASH_NAME_RE = re.compile(r"^proof-([0-9a-f]{12})\.", re.I)


def _config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def proof_channel_id() -> int:
    env = os.getenv("PAYMENT_PROOF_CHANNEL_ID", "").strip()
    if env.isdigit():
        return int(env)
    raw = str((_config().get("channel_links") or {}).get("payout_proofs", ""))
    return int(raw) if raw.isdigit() else 0


def interval() -> timedelta:
    raw = os.getenv("PAYMENT_PROOF_INTERVAL_HOURS", "").strip()
    try:
        hours = float(raw) if raw else 48.0
    except ValueError:
        hours = 48.0
    return timedelta(hours=hours)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _proof_name(data: bytes, suffix: str) -> str:
    suffix = suffix.lower() if suffix.lower() in IMAGE_EXTENSIONS else ".png"
    return f"proof-{_digest(data)}{suffix}"


async def _channel(client: discord.Client) -> discord.TextChannel:
    channel_id = proof_channel_id()
    channel = client.get_channel(channel_id)
    if channel is None and channel_id:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            channel = None
    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError(f"Payment proof channel {channel_id or '(not set)'} not found.")
    return channel


async def _channel_state(channel: discord.TextChannel, bot_id: int) -> tuple[set[str], set[str], datetime | None]:
    """Return (posted file names, posted hashes, time of the latest bot image)."""
    names: set[str] = set()
    hashes: set[str] = set()
    last: datetime | None = None
    async for message in channel.history(limit=HISTORY_LIMIT):
        if message.author.id != bot_id:
            continue
        images = [a for a in message.attachments if a.filename.lower().endswith(IMAGE_EXTENSIONS)]
        if not images:
            continue
        if last is None or message.created_at > last:
            last = message.created_at
        for attachment in images:
            names.add(attachment.filename.lower())
            match = HASH_NAME_RE.match(attachment.filename)
            if match:
                hashes.add(match.group(1).lower())
    return names, hashes, last


def _pending(names: set[str], hashes: set[str]) -> list[tuple[Path, bytes]]:
    pending: list[tuple[Path, bytes]] = []
    for path in list_proof_image_paths():
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if path.name.lower() in names:
            continue
        data = path.read_bytes()
        if _digest(data) in hashes:
            continue
        pending.append((path, data))
    return pending


_post_lock = asyncio.Lock()


async def post_next_proof(client: discord.Client, *, force: bool = False) -> dict | None:
    """Post the next unposted folder proof. Without force, waits for the interval."""
    async with _post_lock:
        channel = await _channel(client)
        names, hashes, last = await _channel_state(channel, client.user.id)
        if not force and last is not None and discord.utils.utcnow() - last < interval():
            return None
        pending = _pending(names, hashes)
        if not pending:
            return None
        path, data = pending[0]
        await channel.send(file=discord.File(io.BytesIO(data), filename=_proof_name(data, path.suffix)))
        print(f"[PAYMENT-PROOF] Posted {path.name} in #{channel.name} ({len(pending) - 1} left)")
        return {"name": path.name, "channel": channel, "remaining": len(pending) - 1}


async def post_uploaded_proofs(client: discord.Client, uploads: list[discord.Attachment]) -> discord.TextChannel:
    async with _post_lock:
        channel = await _channel(client)
        for upload in uploads:
            data = await upload.read()
            suffix = Path(upload.filename).suffix or ".png"
            await channel.send(file=discord.File(io.BytesIO(data), filename=_proof_name(data, suffix)))
        print(f"[PAYMENT-PROOF] Posted {len(uploads)} uploaded proof(s) in #{channel.name}")
        return channel


_drip_task: asyncio.Task | None = None


def start_proof_drip(client: discord.Client) -> None:
    global _drip_task
    if _drip_task is not None and not _drip_task.done():
        return

    async def _loop() -> None:
        try:
            channel = await _channel(client)
            names, hashes, last = await _channel_state(channel, client.user.id)
            next_at = (last + interval()).isoformat() if last else "now"
            print(f"[PAYMENT-PROOF] {len(_pending(names, hashes))} proof(s) left to post · next at {next_at}")
        except Exception as exc:
            print(f"[PAYMENT-PROOF] Startup check failed: {exc}")
        while True:
            try:
                await post_next_proof(client)
            except Exception as exc:
                print(f"[PAYMENT-PROOF] Post failed: {exc}")
            await asyncio.sleep(CHECK_EVERY_SECONDS)

    _drip_task = asyncio.create_task(_loop())


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


def _is_image(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(IMAGE_EXTENSIONS)


@app_commands.command(
    name="post-proof",
    description="Staff : publie des preuves de paiement maintenant (jusqu’à 10 images, ou la suivante du dossier).",
)
@app_commands.describe(
    image1="Preuve n°1 (publiée dans cet ordre)",
    image2="Preuve n°2",
    image3="Preuve n°3",
    image4="Preuve n°4",
    image5="Preuve n°5",
    image6="Preuve n°6",
    image7="Preuve n°7",
    image8="Preuve n°8",
    image9="Preuve n°9",
    image10="Preuve n°10",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def post_proof_command(
    interaction: discord.Interaction,
    image1: discord.Attachment | None = None,
    image2: discord.Attachment | None = None,
    image3: discord.Attachment | None = None,
    image4: discord.Attachment | None = None,
    image5: discord.Attachment | None = None,
    image6: discord.Attachment | None = None,
    image7: discord.Attachment | None = None,
    image8: discord.Attachment | None = None,
    image9: discord.Attachment | None = None,
    image10: discord.Attachment | None = None,
) -> None:
    if not is_staff(interaction):
        await interaction.response.send_message("Cette commande est réservée au staff.", ephemeral=True)
        return
    uploads = [a for a in (image1, image2, image3, image4, image5, image6, image7, image8, image9, image10) if a]
    if any(not _is_image(a) for a in uploads):
        await interaction.response.send_message("Envoie uniquement des images (PNG, JPG ou WebP).", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    hours = int(interval().total_seconds() // 3600)
    try:
        if uploads:
            channel = await post_uploaded_proofs(interaction.client, uploads)
            await interaction.followup.send(
                f"✅ {len(uploads)} preuve(s) publiée(s) dans {channel.mention}, dans l’ordre. "
                f"Prochaine preuve automatique dans {hours} h.",
                ephemeral=True,
            )
            return
        result = await post_next_proof(interaction.client, force=True)
    except (RuntimeError, discord.HTTPException) as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        return
    if result:
        msg = (
            f"✅ `{result['name']}` publiée dans {result['channel'].mention}. "
            f"{result['remaining']} restante(s), la prochaine automatiquement dans {hours} h."
        )
    else:
        msg = "Rien à publier : toutes les preuves du dossier ont déjà été publiées. Joins des images à la commande."
    await interaction.followup.send(msg, ephemeral=True)


@app_commands.command(name="say", description="Staff : envoie un message en tant que bot.")
@app_commands.describe(
    salon="Où publier",
    message="Texte (utilise \\n pour un retour à la ligne)",
    image="Image ou fichier à joindre",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def say_command(
    interaction: discord.Interaction,
    salon: discord.TextChannel,
    message: app_commands.Range[str, 1, 2000] | None = None,
    image: discord.Attachment | None = None,
) -> None:
    if not is_staff(interaction):
        await interaction.response.send_message("Cette commande est réservée au staff.", ephemeral=True)
        return
    text = (message or "").replace("\\n", "\n")
    if not text and image is None:
        await interaction.response.send_message("Ajoute un message, une image, ou les deux.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        file = discord.File(io.BytesIO(await image.read()), filename=image.filename) if image else None
        await salon.send(
            content=text or None,
            file=file or discord.utils.MISSING,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
    except discord.HTTPException as exc:
        await interaction.followup.send(f"❌ Envoi impossible : {exc}", ephemeral=True)
        return
    await interaction.followup.send(f"✅ Envoyé dans {salon.mention}.", ephemeral=True)
