import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from embed_utils import chunk_message
from embeds import MESSAGE_TEMPLATES
from publisher import load_channel_config, publish_channel
from content_generator import ContentGeneratorView
from payout_submission import PayoutSubmitView, PayoutTicketView
from registration import RegisterView

load_dotenv(override=True)


def _clean_env(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().strip('"').strip("'")


TOKEN = _clean_env(os.getenv("DISCORD_TOKEN"))
AUTO_PUBLISH = _clean_env(os.getenv("AUTO_PUBLISH_ON_START", "false")).lower() in (
    "1",
    "true",
    "yes",
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
_webhook_runner = None

# Guilds where slash commands are instantly synced (JobShift Post + JobShift Creators).
SLASH_COMMAND_GUILD_IDS = [1546459225260298240, 1540823867818385468]


async def publish_all_channels() -> None:
    """Publie le bon template dans chaque salon configuré."""
    mapping = load_channel_config()
    if not mapping:
        print("Aucun salon dans channel_config.json")
        return

    for channel_id, template_name in mapping.items():
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            print(f"Salon {channel_id} introuvable (template: {template_name})")
            continue
        try:
            updated = await publish_channel(channel, template_name, bot.user)
            if updated:
                print(f"Updated '{template_name}' in #{channel.name}")
            else:
                print(f"Skipped '{template_name}' in #{channel.name} (unchanged)")
        except Exception as exc:
            print(f"Erreur salon {channel_id}: {exc}")


@bot.event
async def on_ready():
    global _webhook_runner

    print(f"Connecté en tant que {bot.user} (ID: {bot.user.id})")
    print("------")

    bot.add_view(RegisterView())
    bot.add_view(PayoutSubmitView())
    bot.add_view(PayoutTicketView())
    bot.add_view(ContentGeneratorView())

    if _webhook_runner is None:
        from creator_signup_webhook import start_creator_signup_webhook

        _webhook_runner = await start_creator_signup_webhook(bot)

    for guild_id in SLASH_COMMAND_GUILD_IDS:
        guild_obj = discord.Object(id=guild_id)
        try:
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"Synced {len(synced)} slash command(s) to guild {guild_id}")
        except discord.HTTPException as exc:
            print(f"Slash command sync failed for guild {guild_id}: {exc}")

    if AUTO_PUBLISH:
        await publish_all_channels()


@bot.event
async def on_member_join(member: discord.Member):
    from activity_logs import log_member_join

    try:
        await log_member_join(bot, member)
    except Exception as exc:
        print(f"Join log failed for {member.id}: {exc}")


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.send("Pong !")


@bot.command(name="refresh")
async def refresh(ctx: commands.Context, template_name: str | None = None):
    """
    Update this channel only if content changed (in-place edit when possible).
    Usage: !refresh | !refresh account_setup | !refresh force
    """
    mapping = load_channel_config()
    name = template_name or mapping.get(str(ctx.channel.id))
    force = False

    if name:
        parts = name.split()
        force = "force" in parts
        parts = [part for part in parts if part != "force"]
        name = parts[0] if parts else mapping.get(str(ctx.channel.id))

    if not name:
        await ctx.send(
            "Aucun template pour ce salon. Ajoute l’ID dans `channel_config.json` "
            "ou lance : `!refresh account_setup`"
        )
        return

    from publisher import _load_special_publishers

    known = set(MESSAGE_TEMPLATES) | set(_load_special_publishers())
    if name not in known:
        await ctx.send(f"Template `{name}` introuvable.")
        return

    try:
        updated = await publish_channel(ctx.channel, name, bot.user, force=force)
    except Exception as exc:
        await ctx.send(f"Publication échouée : {exc}", delete_after=10)
        return

    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    if not updated:
        await ctx.send(
            "Aucun changement. Utilise `!refresh force` ou `!fixchannels` (admin) pour republier.",
            delete_after=6,
        )
        return


@bot.command(name="refreshall")
@commands.has_permissions(administrator=True)
async def refresh_all(ctx: commands.Context):
    """Sync all configured channels (only changed content). Add 'force' to repost all."""
    force = "force" in (ctx.message.content or "").lower()
    mapping = load_channel_config()
    updated_count = 0
    skipped_count = 0
    errors: list[str] = []

    for channel_id, template_name in mapping.items():
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            errors.append(f"<#{channel_id}> : salon introuvable")
            continue
        try:
            if await publish_channel(channel, template_name, bot.user, force=force):
                updated_count += 1
            else:
                skipped_count += 1
        except Exception as exc:
            errors.append(f"<#{channel_id}> (`{template_name}`): {exc}")

    summary = f"Terminé — {updated_count} mis à jour, {skipped_count} inchangés."
    if errors:
        summary += "\n\n**Erreurs :**\n" + "\n".join(errors[:8])
    await ctx.send(summary, delete_after=15 if errors else 6)


@bot.command(name="fixchannels")
@commands.has_permissions(administrator=True)
async def fix_channels(ctx: commands.Context):
    """Clear publish cache and repost every configured channel."""
    from publish_sync import clear_publish_state

    clear_publish_state()
    mapping = load_channel_config()
    updated = 0
    errors: list[str] = []

    for channel_id, template_name in mapping.items():
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            errors.append(f"<#{channel_id}> : introuvable")
            continue
        try:
            await publish_channel(channel, template_name, bot.user, force=True)
            updated += 1
        except Exception as exc:
            errors.append(f"<#{channel_id}>: {exc}")

    msg = f"**{updated}** salon(s) republié(s)."
    if errors:
        msg += "\n" + "\n".join(errors[:8])
    await ctx.send(msg, delete_after=12)
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass


@bot.command(name="say")
async def say(ctx: commands.Context, *, message: str):
    """Texte simple, découpé automatiquement si > 2000 caractères."""
    for part in chunk_message(message):
        await ctx.send(part)


@bot.command(name="postregister")
@commands.has_permissions(administrator=True)
async def post_register(ctx: commands.Context):
    """Publie le message d'inscription + bouton Register dans ce salon."""
    from registration import REGISTRATION_TEMPLATE

    await publish_channel(ctx.channel, REGISTRATION_TEMPLATE, bot.user)
    await ctx.message.delete()


@bot.command(name="postpayout")
@commands.has_permissions(administrator=True)
async def post_payout(ctx: commands.Context):
    """Publie le message payout + bouton Submit Payout dans ce salon."""
    from payout_submission import PAYOUT_SUBMISSION_TEMPLATE

    await publish_channel(ctx.channel, PAYOUT_SUBMISSION_TEMPLATE, bot.user)
    await ctx.message.delete()


@bot.command(name="postproofs")
@commands.has_permissions(administrator=True)
async def post_proofs(ctx: commands.Context):
    """Publie les screenshots de preuves de payout dans ce salon."""
    from payout_proofs import PAYOUT_PROOFS_TEMPLATE

    await publish_channel(ctx.channel, PAYOUT_PROOFS_TEMPLATE, bot.user, force=True)
    await ctx.message.delete()


@bot.command(name="testcreatorsignup")
@commands.has_permissions(administrator=True)
async def test_creator_signup(ctx: commands.Context):
    """Send a test creator signup notification to the Creators log channel."""
    from creator_signups import CreatorSignup, log_creator_signup

    signup = CreatorSignup(
        email="marie.dupont@example.com",
        country="France",
        region="Île-de-France",
        city="Paris",
        accounts=(
            "• **Instagram:** @marie.jobsearch\n"
            "• **TikTok:** @marie.career.tips"
        ),
        user_id="test-user-001",
        full_name="Marie Dupont",
        signed_up_at="2026-09-12T08:22:00Z",
    )

    message = await log_creator_signup(bot, signup)
    if message is None:
        await ctx.send(
            "Could not send notification — check `creator_signup_log_channel` in "
            "`channel_config.json` and bot access to the Creators server.",
            delete_after=12,
        )
        return

    await ctx.send(f"Test notification sent: {message.jump_url}", delete_after=12)
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass


@bot.command(name="postcontent")
@commands.has_permissions(administrator=True)
async def post_content(ctx: commands.Context):
    """Publie le panneau Content Generator dans ce salon."""
    from content_generator import CONTENT_GENERATOR_TEMPLATE

    await publish_channel(ctx.channel, CONTENT_GENERATOR_TEMPLATE, bot.user)
    await ctx.message.delete()


@bot.command(name="templates")
async def list_templates(ctx: commands.Context):
    """Liste les templates disponibles."""
    from publisher import _load_special_publishers

    all_names = sorted(set(MESSAGE_TEMPLATES) | set(_load_special_publishers()))
    names = "\n".join(f"• `{name}`" for name in all_names)
    mapping = load_channel_config()
    channels = "\n".join(
        f"• <#{cid}> → `{tpl}`" for cid, tpl in mapping.items()
    ) or "_(aucun salon configuré)_"

    embed = discord.Embed(
        title="Templates & salons",
        color=0x9B59B6,
    )
    embed.add_field(name="Templates", value=names, inline=False)
    embed.add_field(name="Salons configurés", value=channels, inline=False)
    await ctx.send(embed=embed)


VIDEO_CONTENT_TYPES = ("video/mp4", "video/quicktime")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def _looks_like_video(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.split(";")[0] in VIDEO_CONTENT_TYPES:
        return True
    return attachment.filename.lower().endswith(VIDEO_EXTENSIONS)


def _looks_like_image(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(IMAGE_EXTENSIONS)


@bot.tree.command(
    name="videoinfo",
    description="Affiche les métadonnées d'identité présentes dans une vidéo (avant nettoyage).",
)
@app_commands.describe(video="Fichier vidéo (mp4/mov) à analyser")
async def videoinfo_command(interaction: discord.Interaction, video: discord.Attachment):
    if not _looks_like_video(video):
        await interaction.response.send_message(
            "Envoie un fichier `.mp4` ou `.mov`.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    import tempfile
    from pathlib import Path

    from video_metadata import (
        VideoMetadataError,
        collect_identity_tags,
        probe_json,
    )

    tmp_dir = Path(tempfile.mkdtemp(prefix="videoinfo_"))
    input_path = tmp_dir / video.filename
    try:
        await video.save(input_path)
        probe = await bot.loop.run_in_executor(None, probe_json, input_path)
        tags = collect_identity_tags(probe)
    except VideoMetadataError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        return
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)

    embed = discord.Embed(
        title="🔍 Métadonnées détectées",
        color=0xE67E22,
    )
    if tags:
        body = "\n".join(f"• `{k}` = `{v}`" for k, v in list(tags.items())[:20])
        embed.add_field(name=f"{len(tags)} champ(s) trouvé(s)", value=body[:1024], inline=False)
    else:
        embed.add_field(
            name="Aucune métadonnée d'identité trouvée",
            value="Le fichier semble déjà propre (ou déjà re-encodé par une plateforme).",
            inline=False,
        )
    embed.set_footer(text="Utilise /cleanvideo pour nettoyer + varier ce fichier.")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="cleanvideo",
    description="Supprime les métadonnées d'identité et applique une variation subtile à une vidéo.",
)
@app_commands.describe(
    video="Fichier vidéo (mp4/mov) à nettoyer",
    variation="Appliquer aussi une variation visuelle subtile (zoom/couleur/vitesse) — recommandé",
    iphone_signature="Injecter une signature 'filmé à l'iPhone 15 Pro, à l'instant' (recommandé)",
)
async def cleanvideo_command(
    interaction: discord.Interaction,
    video: discord.Attachment,
    variation: bool = True,
    iphone_signature: bool = True,
):
    if not _looks_like_video(video):
        await interaction.response.send_message(
            "Envoie un fichier `.mp4` ou `.mov`.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    import random
    import shutil
    import tempfile
    from pathlib import Path

    from clip_assembler import ClipAssemblyError, prepare_for_discord_upload
    from video_metadata import (
        VideoMetadataError,
        clean_and_vary_video,
        inject_iphone_signature,
        variation_summary,
    )

    tmp_dir = Path(tempfile.mkdtemp(prefix="cleanvideo_"))
    input_path = tmp_dir / video.filename
    output_path = tmp_dir / "cleaned.mp4"

    try:
        await video.save(input_path)

        result = await bot.loop.run_in_executor(
            None,
            lambda: clean_and_vary_video(
                input_path, output_path, apply_variation=variation
            ),
        )

        final_path = await bot.loop.run_in_executor(
            None, prepare_for_discord_upload, result.output_path
        )

        if iphone_signature:
            final_path = await bot.loop.run_in_executor(
                None, lambda: inject_iphone_signature(final_path)
            )

        embed = discord.Embed(title="✅ Vidéo nettoyée", color=0x57F287)
        embed.add_field(
            name="Métadonnées supprimées",
            value=f"{len(result.tags_removed)} champ(s)" if result.tags_removed else "Aucune trouvée",
            inline=True,
        )
        embed.add_field(
            name="Taille",
            value=f"{result.size_before // 1024} Ko → {final_path.stat().st_size // 1024} Ko",
            inline=True,
        )
        if variation:
            embed.add_field(
                name="Variations appliquées",
                value=variation_summary(result.variation),
                inline=False,
            )
        else:
            embed.add_field(
                name="Variations",
                value="Désactivées (nettoyage métadonnées uniquement)",
                inline=False,
            )
        if iphone_signature:
            embed.add_field(
                name="Signature injectée",
                value="📱 iPhone 15 Pro · iOS 26.3 · date fraîche (à l'instant)",
                inline=False,
            )

        # Real Camera.app exports are named IMG_####.MOV — avoid "clean_*"
        # which looks like an edited/processed file.
        if iphone_signature:
            out_name = f"IMG_{random.randint(1000, 9999)}.MOV"
        else:
            out_name = f"IMG_{random.randint(1000, 9999)}{final_path.suffix}"

        await interaction.followup.send(
            embed=embed,
            file=discord.File(final_path, filename=out_name),
            ephemeral=True,
        )
    except (VideoMetadataError, ClipAssemblyError) as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
    except discord.HTTPException as exc:
        await interaction.followup.send(f"❌ Envoi Discord échoué : {exc}", ephemeral=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@bot.tree.command(
    name="varyimage",
    description="Crée une variation d'une image (recadrage/couleur, ou IA si FAL_KEY est configuré).",
)
@app_commands.describe(
    image="Image à varier",
    ai="Utiliser l'IA (fal.ai) si disponible plutôt que le recadrage/couleur local",
)
async def varyimage_command(
    interaction: discord.Interaction,
    image: discord.Attachment,
    ai: bool = False,
):
    if not _looks_like_image(image):
        await interaction.response.send_message(
            "Envoie une image `.png` / `.jpg` / `.webp`.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    import random
    import shutil
    import tempfile
    from pathlib import Path

    from PIL import Image

    tmp_dir = Path(tempfile.mkdtemp(prefix="varyimage_"))
    input_path = tmp_dir / image.filename
    output_path = tmp_dir / "varied.png"

    try:
        await image.save(input_path)
        img = await bot.loop.run_in_executor(None, lambda: Image.open(input_path).convert("RGB"))

        from carousel.generate_carousel import _augment_ai, _augment_crop, _augment_grade

        rng = random.Random()
        method_used = "ai" if ai else "crop+grade"

        def _run_augment():
            if ai:
                return _augment_ai(img, rng)
            return _augment_grade(_augment_crop(img, rng), rng)

        varied = await bot.loop.run_in_executor(None, _run_augment)
        await bot.loop.run_in_executor(None, lambda: varied.save(output_path, quality=95))

        embed = discord.Embed(
            title="✅ Variation générée",
            description=f"Méthode : `{method_used}`" + ("" if ai else " (locale, sans API)"),
            color=0x57F287,
        )
        await interaction.followup.send(
            embed=embed,
            file=discord.File(output_path, filename=f"varied_{image.filename}"),
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _validate_token(token: str) -> None:
    if token.isdigit():
        raise SystemExit(
            "DISCORD_TOKEN ressemble à un ID d'application. "
            "Va sur discord.com/developers → Bot → Reset Token."
        )
    if len(token) == 64 and all(c in "0123456789abcdef" for c in token.lower()):
        raise SystemExit(
            "DISCORD_TOKEN ressemble à la clé publique. "
            "Utilise le token sous Bot → Reset Token."
        )
    if token.count(".") != 2:
        raise SystemExit(
            "DISCORD_TOKEN invalide (format : xxx.yyy.zzz). "
            "Recopie le token sans guillemets ni espaces."
        )


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN missing in .env")

    _validate_token(TOKEN)

    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        raise SystemExit(
            "DISCORD_TOKEN rejected by Discord (401). "
            "Run: python check_token.py — then reset the token in the Developer Portal."
        ) from None
