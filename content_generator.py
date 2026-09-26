import asyncio
import json
import re
from pathlib import Path

import discord

from channel_utils import delete_bot_messages
from embeds import channel_mention

CONTENT_GENERATOR_TEMPLATE = "content_generator_welcome"
CONTENT_COLOR = 0x57F287  # green accent like reference panel
PROGRESS_COLOR = 0x5865F2  # blurple progress embed like reference bot
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


def _sanitize_username(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]", "", name.lower().replace(" ", "-"))
    return (cleaned or "user")[:90]


def thread_name_for(user: discord.User) -> str:
    return f"clips-{_sanitize_username(user.name)}"


def progress_embed(current: int, total: int) -> discord.Embed:
    return discord.Embed(
        description=f"⏳ **Génération du carrousel {current}/{total}…**",
        color=PROGRESS_COLOR,
    )


def batch_complete_embed(generated: int, requested: int, thread: discord.Thread) -> discord.Embed:
    if requested > 1:
        heading = f"✅ **Lot terminé — {generated}/{requested} carrousels**"
    else:
        heading = f"✅ **Terminé — {generated}/{requested} carrousel**"
    return discord.Embed(
        description=(
            f"{heading}\n"
            f"Publié dans ton fil privé → {thread.mention}"
        ),
        color=CONTENT_COLOR,
    )


def panel_embed() -> discord.Embed:
    music = channel_mention("music")
    return discord.Embed(
        title="🎬 Générateur de contenu",
        description=(
            "Clique sur un bouton ci-dessous pour générer un carrousel "
            "(accroche, méthode, ou liste d’entreprises).\n\n"
            "**Générer du contenu** — 1 carrousel\n"
            "**Générer un lot** — jusqu’à 5 carrousels (accroches, textes, CTA et photos différents)\n\n"
            "Tu choisis le template. L’avatar reste le même.\n\n"
            f"⭐ Avant de poster : ajoute tous les sons en favoris dans {music} "
            "pour les retrouver au moment de publier le carrousel."
        ),
        color=CONTENT_COLOR,
    )


def thread_welcome_embed(user: discord.User, mode: str) -> discord.Embed:
    mode_label = "Générer un lot" if mode == "batch" else "Générer du contenu"
    return discord.Embed(
        title="🎬 Ton espace posting",
        description=(
            f"Salut {user.mention} — voici ton fil privé de posting.\n\n"
            f"Tu l’as ouvert via **{mode_label}**.\n\n"
            "Tes carrousels apparaîtront ici en album photo, "
            "avec un bouton **Télécharger le ZIP**.\n\n"
            "Prêt à poster en carrousel Instagram / TikTok."
        ),
        color=CONTENT_COLOR,
    )


async def _add_staff_to_thread(thread: discord.Thread) -> None:
    guild = thread.guild
    if guild is None:
        return
    for role_id in _staff_role_ids():
        role = guild.get_role(role_id)
        if role is None:
            continue
        for member in role.members:
            try:
                await thread.add_user(member)
            except discord.HTTPException:
                pass


async def _ensure_thread_member(thread: discord.Thread, user: discord.abc.User) -> None:
    """Private threads hide messages from anyone who wasn't added."""
    if thread.archived:
        try:
            await thread.edit(archived=False)
        except discord.HTTPException:
            pass
    try:
        await thread.add_user(user)
    except discord.HTTPException:
        pass
    await _add_staff_to_thread(thread)


async def find_clips_thread(
    channel: discord.TextChannel,
    user: discord.User,
) -> discord.Thread | None:
    name = thread_name_for(user)
    found: dict[int, discord.Thread] = {}

    for thread in channel.threads:
        if thread.name == name:
            found[thread.id] = thread

    guild = channel.guild
    if guild is not None:
        try:
            active = await guild.fetch_active_threads()
            for thread in active.threads:
                if thread.parent_id == channel.id and thread.name == name:
                    found[thread.id] = thread
        except discord.HTTPException:
            pass

    for private in (False, True):
        try:
            async for thread in channel.archived_threads(
                limit=100,
                private=private,
                joined=private,
            ):
                if thread.name == name:
                    found[thread.id] = thread
                    break
        except discord.HTTPException:
            pass

    if not found:
        return None

    thread = max(found.values(), key=lambda item: item.id)
    await _ensure_thread_member(thread, user)
    return thread


async def get_or_create_clips_thread(
    channel: discord.TextChannel,
    member: discord.Member,
    *,
    mode: str,
) -> discord.Thread:
    existing = await find_clips_thread(channel, member)
    if existing:
        return existing

    thread = await channel.create_thread(
        name=thread_name_for(member),
        type=discord.ChannelType.private_thread,
        invitable=False,
        auto_archive_duration=10080,
        reason=f"Espace posting pour {member}",
    )
    await thread.add_user(member)
    await _add_staff_to_thread(thread)
    await thread.send(embed=thread_welcome_embed(member, mode))
    return thread


async def post_clip_to_thread(
    channel: discord.TextChannel,
    user: discord.User | discord.Member,
    video: discord.Attachment | discord.File | Path,
    *,
    caption_emoji: str = "🎬",
) -> discord.Message:
    """
    Deliver a generated clip to the user's private thread.
    Call this when the generation API is ready.
    """
    member = user if isinstance(user, discord.Member) else None
    if member is None and channel.guild:
        member = channel.guild.get_member(user.id)

    if member is None:
        raise ValueError("Member must be in the guild to resolve their clips thread.")

    thread = await find_clips_thread(channel, user)
    if thread is None:
        thread = await get_or_create_clips_thread(channel, member, mode="single")

    content = f"{user.mention} {caption_emoji}"
    if isinstance(video, Path):
        return await thread.send(content, file=discord.File(video))
    if isinstance(video, discord.File):
        return await thread.send(content, file=video)
    return await thread.send(content, file=await video.to_file())


async def _generate_clips_for_user(
    interaction: discord.Interaction,
    parent_channel: discord.TextChannel,
    member: discord.Member,
    thread: discord.Thread,
    *,
    mode: str,
    count: int,
    progress_message: discord.WebhookMessage,
    avatar_pack: str | None = None,
    carousel_style: str | None = None,
) -> tuple[int, list[str], list[str]]:
    from carousel_assembler import (
        CarouselAssemblyError,
        CarouselRecipe,
        assemble_carousel,
        assets_status,
        cleanup_carousel_artifacts,
        recipe_summary,
    )
    from clip_delivery import deliver_carousel_to_thread

    status = assets_status()
    if status["avatars"] < 1 or status["photos"] < 1:
        return (
            0,
            [
                "Assets carrousel manquants. Ajoute des fichiers dans "
                "`carousel/assets/avatars` et `carousel/assets/photos` "
                f"sur la machine du bot (vérifié : `{status.get('root', '')}` — "
                f"avatars={status['avatars']}, photos={status['photos']})."
            ],
            [],
        )
    if not status["captions"]:
        return (
            0,
            ["Fichier `carousel/carousel_captions.txt` manquant sur la machine du bot."],
            [],
        )

    pending: list[CarouselRecipe] = []
    errors: list[str] = []

    for index in range(count):
        try:
            await progress_message.edit(embed=progress_embed(index + 1, count))
        except discord.HTTPException:
            pass

        try:
            recipe = await asyncio.to_thread(
                assemble_carousel,
                seed=hash((member.id, index, progress_message.id, avatar_pack or "", carousel_style or ""))
                & 0xFFFFFFFF,
                avatar_pack=avatar_pack,
                carousel_style=carousel_style,
            )
            pending.append(recipe)
            except Exception as exc:
                errors.append(f"Carrousel {index + 1} échoué : {exc}")

    created = 0
    external_links: list[str] = []
    delivered_outputs: list = []
    if pending:
        try:
            await progress_message.edit(
                embed=batch_complete_embed(len(pending), count, thread),
            )
        except discord.HTTPException:
            pass

        from activity_logs import ClipOutput

        for index, recipe in enumerate(pending, start=1):
            clip_label = f"Carrousel {index}/{len(pending)}"
            try:
                delivery_mode, url = await deliver_carousel_to_thread(
                    thread,
                    member,
                    recipe.slides,
                    clip_label=clip_label,
                )
                created += 1
                delivered_outputs.append(
                    ClipOutput(
                        label=clip_label,
                        summary=recipe_summary(recipe),
                        delivery_mode=delivery_mode,
                        url=url,
                        slide_paths=list(recipe.slides),
                    )
                )
                if delivery_mode == "external" and url:
                    external_links.append(f"**{clip_label}:** {url}")
            except CarouselAssemblyError as exc:
                errors.append(f"{clip_label} échoué : {exc}")
            except discord.HTTPException as exc:
                errors.append(f"Impossible d’envoyer {clip_label.lower()} : {exc}")

        if external_links:
            await thread.send(
                "🔗 **Téléchargements externes** (limite de fichier Discord) :\n"
                + "\n".join(external_links),
                suppress_embeds=True,
            )

        if created == len(pending) and created == count:
            await thread.send(
                f"✅ Les **{created}** carrousel{'s' if created != 1 else ''} sont prêts."
            )
        elif created == 1:
            await thread.send(
                f"✅ Carrousel prêt — {len(pending[0].slides)} slides au-dessus."
            )
        elif created > 1:
            await thread.send(f"✅ **{created}** carrousels sont prêts.")

    if count > 1 and 0 < created < count:
        errors.insert(0, f"Seuls **{created}/{count}** carrousels ont été livrés.")

    if external_links and not errors:
        try:
            await progress_message.edit(
                embed=discord.Embed(
                    description=(
                        f"✅ **Lot terminé — {created}/{count} carrousels**\n"
                        f"Certaines slides sont des liens externes dans {thread.mention}."
                    ),
                    color=CONTENT_COLOR,
                )
            )
        except discord.HTTPException:
            pass

    if created == 0 and errors:
        try:
            await progress_message.delete()
        except discord.HTTPException:
            pass

    if external_links:
        await interaction.followup.send(
            "🔗 **Téléchargements externes** (trop lourd pour Discord) :\n"
            + "\n".join(external_links),
            ephemeral=True,
        )

    if created > 0 and delivered_outputs and interaction.client:
        from activity_logs import log_content_generation

        try:
            await log_content_generation(
                interaction.client,
                member,
                mode=mode,
                thread=thread,
                outputs=delivered_outputs,
                created=created,
                requested=count,
            )
        except Exception as exc:
            print(f"Content log failed for {member.id}: {exc}")

    # Cleanup after staff log so slides can still be attached there.
    if pending:
        for recipe in pending:
            cleanup_carousel_artifacts(recipe.job_dir)

    return created, errors, external_links


async def _send_ephemeral_errors(
    interaction: discord.Interaction,
    errors: list[str],
) -> None:
    """Keep errors ephemeral so they can be dismissed and the thread stays clean."""
    if not errors:
        return
    body = "\n".join(f"• {line}" for line in errors)
    await interaction.followup.send(
        f"❌ **Génération échouée**\n{body}",
        ephemeral=True,
    )


class _SilentProgress:
    async def edit(self, **kwargs) -> None:
        return None

    async def delete(self) -> None:
        return None


async def _show_status(interaction: discord.Interaction, text: str) -> None:
    body = text[:2000]
    try:
        await interaction.edit_original_response(content=body, view=None, embed=None)
        return
    except discord.HTTPException:
        pass
    try:
        await interaction.followup.send(body, ephemeral=True)
    except discord.HTTPException:
        pass


async def _handle_clip_request(
    interaction: discord.Interaction,
    *,
    mode: str,
    count: int = 1,
    avatar_pack: str | None = None,
    carousel_style: str | None = None,
) -> None:
    if not isinstance(interaction.channel, discord.TextChannel):
        message = "Ce panneau fonctionne uniquement dans un salon texte."
        if interaction.response.is_done():
            await _show_status(interaction, message)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return

    if not isinstance(interaction.user, discord.Member):
        message = "Impossible de vérifier ton appartenance au serveur."
        if interaction.response.is_done():
            await _show_status(interaction, message)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    try:
        thread = await get_or_create_clips_thread(
            interaction.channel,
            interaction.user,
            mode=mode,
        )
    except discord.HTTPException as exc:
        await _show_status(interaction, f"❌ Impossible de créer ton fil carrousel : {exc}")
        return

    count = max(1, min(5, count))
    try:
        progress_message = await interaction.followup.send(
            embed=progress_embed(1, count),
            ephemeral=True,
            wait=True,
        )
    except discord.HTTPException:
        progress_message = _SilentProgress()

    try:
        created, errors, _external = await _generate_clips_for_user(
            interaction,
            interaction.channel,
            interaction.user,
            thread,
            mode=mode,
            count=count,
            progress_message=progress_message,
            avatar_pack=avatar_pack,
            carousel_style=carousel_style,
        )
    except Exception as exc:
        print(f"Content generation crashed: {exc}")
        await _show_status(interaction, f"❌ Génération échouée : {exc}")
        try:
            await thread.send(f"❌ Génération échouée : {exc}")
        except discord.HTTPException:
            pass
        return

    if created:
        note = ""
        if errors:
            note = "\n" + "\n".join(f"• {line}" for line in errors)
        await _show_status(
            interaction,
            f"✅ Posté dans {thread.mention}.{note}",
        )
        return

    body = "\n".join(f"• {line}" for line in errors) or "Aucun carrousel produit."
    await _show_status(interaction, f"❌ Génération échouée\n{body}")
    try:
        await thread.send(f"❌ Génération échouée\n{body}"[:1900])
    except discord.HTTPException:
        pass


# User who can pick among all avatar packs (others stay on femme_noir).
AVATAR_PICKER_USER_IDS = {1015244275354706002}

AVATAR_PACK_LABELS = {
    "femme_noir": "Femme noir",
    "femme_noir_2": "Femme noir 2",
    "rousse": "Rousse",
    "tisme_sucre": "Tisme sucré (homme)",
    "homme_blanc": "Homme blanc",
}


def _can_pick_avatar_pack(user_id: int) -> bool:
    return user_id in AVATAR_PICKER_USER_IDS


def _avatar_pack_options() -> list[discord.SelectOption]:
    from carousel.generate_carousel import DEFAULT_AVATAR_PACK, _list_avatar_packs

    packs = _list_avatar_packs() or [DEFAULT_AVATAR_PACK]
    options: list[discord.SelectOption] = []
    for pack in packs:
        label = AVATAR_PACK_LABELS.get(pack, pack.replace("_", " ").title())
        options.append(
            discord.SelectOption(
                label=label[:100],
                value=pack,
                description=f"Pack `{pack}`"[:100],
                default=(pack == DEFAULT_AVATAR_PACK),
            )
        )
    return options[:25]


class AvatarPackSelect(discord.ui.Select):
    def __init__(self, *, mode: str, count: int) -> None:
        self.mode = mode
        self.count = count
        super().__init__(
            placeholder="Choisis un pack d’avatars…",
            min_values=1,
            max_values=1,
            options=_avatar_pack_options(),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        pack = self.values[0]
        label = AVATAR_PACK_LABELS.get(pack, pack)
        await interaction.response.edit_message(
            content=f"Pack **{label}**. Choisis le template à générer :",
            view=TemplatePickView(mode=self.mode, count=self.count, avatar_pack=pack),
        )


class TemplateSelect(discord.ui.Select):
    def __init__(self, *, mode: str, count: int, avatar_pack: str) -> None:
        self.mode = mode
        self.count = count
        self.avatar_pack = avatar_pack
        super().__init__(
            placeholder="Choisis un template…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Template 1 — Google / JobShift",
                    value="template1",
                    description="Méthode classique, pastilles roses",
                ),
                discord.SelectOption(
                    label="Template 2 — Édition rentrée",
                    value="template2",
                    description="POV RH, pastilles crème",
                ),
                discord.SelectOption(
                    label="Template 3 — Entreprises",
                    value="template3",
                    description="3 à 5 boîtes, fond ville de nuit",
                ),
                discord.SelectOption(
                    label="Template 4 — Notes",
                    value="template4",
                    description="Liste par secteur, fond noir",
                ),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        style = self.values[0]
        chosen = next((opt.label for opt in self.options if opt.value == style), style)
        self.disabled = True
        if self.view is not None:
            for item in self.view.children:
                item.disabled = True
            try:
                await interaction.response.edit_message(
                    content=f"Template **{chosen}** — génération…",
                    view=self.view,
                )
            except discord.HTTPException:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
        await _handle_clip_request(
            interaction,
            mode=self.mode,
            count=self.count,
            avatar_pack=self.avatar_pack,
            carousel_style=style,
        )


class TemplatePickView(discord.ui.View):
    def __init__(self, *, mode: str, count: int, avatar_pack: str) -> None:
        super().__init__(timeout=180)
        self.add_item(TemplateSelect(mode=mode, count=count, avatar_pack=avatar_pack))


class AvatarPackPickView(discord.ui.View):
    def __init__(self, *, mode: str, count: int = 1) -> None:
        super().__init__(timeout=180)
        self.add_item(AvatarPackSelect(mode=mode, count=count))


async def _prompt_or_generate(
    interaction: discord.Interaction,
    *,
    mode: str,
    count: int = 1,
) -> None:
    """Everyone picks a template. Avatar stays femme_noir unless staff chose a pack."""
    from carousel.generate_carousel import DEFAULT_AVATAR_PACK

    if _can_pick_avatar_pack(interaction.user.id):
        await interaction.response.send_message(
            "Choisis le pack d’avatars pour ce carrousel :",
            view=AvatarPackPickView(mode=mode, count=count),
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        "Choisis le template. L’avatar reste le même.",
        view=TemplatePickView(
            mode=mode,
            count=count,
            avatar_pack=DEFAULT_AVATAR_PACK,
        ),
        ephemeral=True,
    )


class BatchGenerateModal(discord.ui.Modal, title="Générer un lot"):
    video_count = discord.ui.TextInput(
        label="Combien de carrousels ? (max 5)",
        placeholder="1",
        default="1",
        required=True,
        min_length=1,
        max_length=1,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.video_count.value.strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                "Entre un nombre entier entre 1 et 5.",
                ephemeral=True,
            )
            return

        count = int(raw)
        if count < 1 or count > 5:
            await interaction.response.send_message(
                "Entre un nombre entier entre 1 et 5.",
                ephemeral=True,
            )
            return

        await _prompt_or_generate(interaction, mode="batch", count=count)


class ContentGeneratorView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Générer du contenu",
        style=discord.ButtonStyle.success,
        emoji="🎬",
        custom_id="jelly_content_generate",
    )
    async def generate_content(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await _prompt_or_generate(interaction, mode="single", count=1)

    @discord.ui.button(
        label="Générer un lot",
        style=discord.ButtonStyle.primary,
        emoji="📦",
        custom_id="jelly_content_batch",
    )
    async def batch_generate(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(BatchGenerateModal())


def content_generator_panel_fingerprint() -> str:
    from publish_sync import _embed_payload, _hash_payload

    return _hash_payload(
        {
            "embed": _embed_payload(panel_embed()),
            "buttons": ["jelly_content_generate", "jelly_content_batch"],
        }
    )


async def publish_content_generator_welcome(
    channel: discord.TextChannel,
    bot_user: discord.ClientUser,
    *,
    force: bool = False,
) -> list[int]:
    if force:
        await delete_bot_messages(channel, bot_user)
        sent = await channel.send(embed=panel_embed(), view=ContentGeneratorView())
        return [sent.id]

    from publish_sync import sync_embed_messages

    return await sync_embed_messages(
        channel, bot_user, [panel_embed()], view=ContentGeneratorView()
    )
