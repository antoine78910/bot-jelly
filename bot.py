import os

import discord
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
    print(f"Connecté en tant que {bot.user} (ID: {bot.user.id})")
    print("------")

    bot.add_view(RegisterView())
    bot.add_view(PayoutSubmitView())
    bot.add_view(PayoutTicketView())
    bot.add_view(ContentGeneratorView())

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
            "No template for this channel. Add the ID in `channel_config.json` "
            "or run: `!refresh account_setup`"
        )
        return

    from publisher import _load_special_publishers

    known = set(MESSAGE_TEMPLATES) | set(_load_special_publishers())
    if name not in known:
        await ctx.send(f"Template `{name}` not found.")
        return

    try:
        updated = await publish_channel(ctx.channel, name, bot.user, force=force)
    except Exception as exc:
        await ctx.send(f"Publish failed: {exc}", delete_after=10)
        return

    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    if not updated:
        await ctx.send(
            "No changes detected. Use `!refresh force` or `!fixchannels` (admin) to repost.",
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
            errors.append(f"<#{channel_id}>: channel not found")
            continue
        try:
            if await publish_channel(channel, template_name, bot.user, force=force):
                updated_count += 1
            else:
                skipped_count += 1
        except Exception as exc:
            errors.append(f"<#{channel_id}> (`{template_name}`): {exc}")

    summary = f"Done — {updated_count} updated, {skipped_count} unchanged."
    if errors:
        summary += "\n\n**Errors:**\n" + "\n".join(errors[:8])
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
            errors.append(f"<#{channel_id}>: not found")
            continue
        try:
            await publish_channel(channel, template_name, bot.user, force=True)
            updated += 1
        except Exception as exc:
            errors.append(f"<#{channel_id}>: {exc}")

    msg = f"Reposted **{updated}** channel(s)."
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

    await publish_channel(ctx.channel, PAYOUT_PROOFS_TEMPLATE, bot.user)
    await ctx.message.delete()


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
