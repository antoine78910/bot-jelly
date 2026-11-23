"""Vidéo button and staff commands: one guarded unique video per click, self-hosted.

Flow for a creator click on « Vidéo » (custom_id jelly_content_video):
  creator gate -> readiness (tools, /v routes, public URL, store) -> duplicate and
  capacity check -> private clips thread -> reservation in the store -> queue.
The single worker renders off the event loop, publishes a /v/<token> link, posts
it in the private thread, and only then marks the assignment delivered. Any
failure before that releases the reservation, so it does not count for rotation.

Staff: /add-video, /list-videos, /disable-video (runtime is_staff check, on top of
the default Manage Server permission).

The JSON store and the in-memory queue assume ONE bot replica.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import shutil
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import discord
from discord import app_commands

import video_hosting
from staff import is_staff
from video_store import AlreadyReserved, NoActiveVideo, StoreUnavailable, get_store
from video_uniquify import media, pipeline

VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v")
TITLE_MAX = 80
NOTHING_COUNTED = "Rien n’a été compté"


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name, "").strip()
    return max(lo, min(hi, int(raw))) if raw.isdigit() else default


def max_upload_bytes() -> int:
    return _env_int("VIDEO_MAX_UPLOAD_MB", 300, 1, 2000) * 1024 * 1024


def queue_capacity() -> int:
    return _env_int("VIDEO_QUEUE_MAX", 10, 1, 100)


def safe_text(text: str) -> str:
    """User or staff text shown in Discord: no mentions, no markdown."""
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


NO_MENTIONS = discord.AllowedMentions.none()


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

@dataclass(eq=False)
class VideoJob:
    assignment_id: str
    clipper_id: int
    video_id: str
    seed: int
    pitch_slot: int
    member: discord.abc.User
    thread: discord.abc.Messageable
    interaction: discord.Interaction | None = None
    enqueued_at: float = field(default_factory=time.time)
    cancel: threading.Event = field(default_factory=threading.Event)
    outcome: str = ""
    report: dict = field(default_factory=dict)
    url: str = ""


class VideoQueue:
    """One worker (concurrency 1), bounded, one pending request per creator."""

    def __init__(self, process, *, capacity: int) -> None:
        self._process = process
        self.capacity = capacity
        self._queue: asyncio.Queue | None = None
        self._claims: set[int] = set()
        self._waiting: list[VideoJob] = []
        self.running: VideoJob | None = None
        self._task: asyncio.Task | None = None

    # A claim is taken before the first await of a click, so double clicks cannot race.
    def claim(self, user_id: int) -> str:
        if user_id in self._claims:
            return "duplicate"
        if len(self._claims) >= self.capacity:
            return "full"
        self._claims.add(user_id)
        return "ok"

    def unclaim(self, user_id: int) -> None:
        if not any(j.clipper_id == user_id for j in self._waiting) and (
            self.running is None or self.running.clipper_id != user_id
        ):
            self._claims.discard(user_id)

    def position(self, user_id: int) -> int | None:
        """Jobs ahead of this user's job (0 = being rendered or next)."""
        if self.running is not None and self.running.clipper_id == user_id:
            return 0
        for index, job in enumerate(self._waiting):
            if job.clipper_id == user_id:
                return index + (1 if self.running else 0)
        return None

    def backlog(self) -> int:
        return len(self._waiting) + (1 if self.running else 0)

    def put(self, job: VideoJob) -> int:
        if job.clipper_id not in self._claims:
            raise RuntimeError("claim the user before enqueueing")
        if self._queue is None:
            raise RuntimeError("video worker not started")
        ahead = self.backlog()
        self._queue.put_nowait(job)
        self._waiting.append(job)
        return ahead

    @property
    def started(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.started:
            return
        self._queue = asyncio.Queue()
        self._task = asyncio.create_task(self._worker(), name="video-worker")

    async def drain(self) -> None:
        if self._queue is not None:
            await self._queue.join()

    async def stop(self) -> None:
        """Cancel the worker, kill a running ffmpeg, free every waiting reservation."""
        for job in list(self._waiting):
            job.cancel.set()
            _release(job, "cancelled")
        self._waiting.clear()
        if self.running is not None:
            self.running.cancel.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._queue = None
        self._claims.clear()

    async def _worker(self) -> None:
        assert self._queue is not None
        queue = self._queue
        while True:
            job = await queue.get()
            try:
                if job in self._waiting:
                    self._waiting.remove(job)
                if job.cancel.is_set():
                    continue
                self.running = job
                await self._process(job)
            except asyncio.CancelledError:
                job.cancel.set()
                _release(job, "cancelled")
                raise
            except Exception as exc:  # the worker must survive any single job
                print(f"[VIDEO] Job {job.assignment_id} crashed: {exc!r}")
                _release(job, "crash")
            finally:
                self.running = None
                self._claims.discard(job.clipper_id)
                queue.task_done()


def _release(job: VideoJob, reason: str) -> None:
    try:
        get_store().release(job.assignment_id, reason)
    except Exception as exc:
        print(f"[VIDEO] Could not release {job.assignment_id}: {exc!r}")
    if not job.outcome:
        job.outcome = reason


_queue: VideoQueue | None = None
_selfcheck_problems: list[str] | None = None
_started = False


def get_queue() -> VideoQueue:
    global _queue
    if _queue is None:
        _queue = VideoQueue(process_job, capacity=queue_capacity())
    return _queue


def eta_message(ahead: int) -> str:
    if ahead <= 0:
        return "Temps estimé : 1 à 3 min."
    rank = ahead + 1
    return (
        f"Tu es n°{rank} dans la file ({ahead} vidéo{'s' if ahead > 1 else ''} avant toi). "
        f"Temps estimé : {rank} à {3 * rank} min."
    )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

async def _tell(job: VideoJob, text: str) -> None:
    """Failure notes go to the private thread, and to the ephemeral reply when still valid."""
    try:
        await job.thread.send(
            f"{job.member.mention} {text}"[:2000],
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[job.member]),
        )
    except discord.HTTPException as exc:
        print(f"[VIDEO] Could not write in thread for {job.clipper_id}: {exc}")
    if job.interaction is not None:
        try:
            await job.interaction.edit_original_response(content=text[:2000])
        except (discord.HTTPException, ValueError):
            pass


def _download_view(url: str) -> discord.ui.View:
    from clip_delivery import _download_view as view

    return view(url, label="Télécharger la vidéo")


async def run_render(job: VideoJob, source: Path, siblings: list[pipeline.Signature], work_dir: Path):
    """Rendering adapter: the blocking pipeline runs in a worker thread."""
    return await asyncio.to_thread(
        pipeline.render_unique,
        source,
        work_dir,
        seed=job.seed,
        pitch_slot=job.pitch_slot,
        siblings=siblings,
        ref=job.assignment_id,
        cancel=job.cancel,
    )


async def process_job(job: VideoJob) -> None:
    store = get_store()
    assignment = store.get_assignment(job.assignment_id)
    if assignment is None or assignment["status"] != "reserved":
        job.outcome = "stale"
        return
    video = store.get_video(job.video_id)
    if video is None or not video.get("active") or not store.source_path(video).is_file():
        _release(job, "disabled")
        await _tell(job, f"La vidéo source a été retirée par l’équipe avant ta génération. {NOTHING_COUNTED}, reclique sur **Vidéo** pour en recevoir une autre.")
        return
    if not video_hosting.hosting_ready():
        _release(job, "no_public_url")
        await _tell(job, f"❌ Le lien de téléchargement ne peut pas être créé pour le moment. {NOTHING_COUNTED}, préviens l’équipe.")
        return
    try:
        siblings = [pipeline.Signature.from_json(s) for s in store.sibling_signatures(job.video_id)]
    except (StoreUnavailable, KeyError, TypeError, ValueError) as exc:
        print(f"[VIDEO] Sibling signatures unreadable for {job.video_id}: {exc}")
        _release(job, "signatures")
        await _tell(job, f"❌ La génération vidéo est bloquée (données de comparaison illisibles). {NOTHING_COUNTED}, préviens l’équipe.")
        return

    work_dir = store.work_dir / job.assignment_id
    token = None
    try:
        try:
            result = await run_render(job, store.source_path(video), siblings, work_dir)
        except pipeline.GuardFailed as exc:
            job.report = exc.report
            _release(job, "guard")
            print(f"[VIDEO] {job.assignment_id}: guards failed after {pipeline.MAX_ATTEMPTS} attempts")
            await _tell(job, f"❌ Impossible de produire une version assez différente cette fois ({pipeline.MAX_ATTEMPTS} essais). {NOTHING_COUNTED}, reclique sur **Vidéo** dans quelques minutes. Si ça se répète, préviens l’équipe.")
            return
        except media.RenderCancelled:
            _release(job, "cancelled")
            return
        except media.MediaError as exc:
            _release(job, "media")
            print(f"[VIDEO] {job.assignment_id}: render failed: {exc}")
            await _tell(job, f"❌ La génération vidéo a échoué. {NOTHING_COUNTED}, tu peux recliquer sur **Vidéo**.")
            return
        job.report = result.report

        try:
            token, url, expires_at = await asyncio.to_thread(video_hosting.publish_video, result.output)
        except Exception as exc:
            _release(job, "publish")
            print(f"[VIDEO] {job.assignment_id}: publish failed: {exc.__class__.__name__}")  # paths may hold the token
            await _tell(job, f"❌ Le lien de téléchargement n’a pas pu être créé. {NOTHING_COUNTED}, reclique sur **Vidéo**.")
            return

        try:
            await job.thread.send(
                f"{job.member.mention} 🎞️ **Ta vidéo est prête.**\n"
                "Version unique générée pour toi (MP4 1080×1920, 60 images/s). Télécharge-la "
                "hors Discord pour garder la qualité d’origine. "
                f"Lien personnel valable **{video_hosting.LINK_TTL_LABEL}**, ne le partage pas.",
                view=_download_view(url),
                suppress_embeds=True,
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[job.member]),
            )
        except discord.HTTPException as exc:
            video_hosting.unpublish(token)
            _release(job, "send_failed")
            print(f"[VIDEO] {job.assignment_id}: Discord send failed: {exc}")
            if job.interaction is not None:
                try:
                    await job.interaction.edit_original_response(
                        content=f"❌ Impossible d’écrire dans ton fil privé. {NOTHING_COUNTED}, reclique sur **Vidéo**."
                    )
                except (discord.HTTPException, ValueError):
                    pass
            return

        try:
            store.mark_delivered(job.assignment_id, signature=result.signature.to_json(), expires_at=expires_at)
        except Exception as exc:
            # The creator already has the link; keep it, but say loudly that rotation missed it.
            print(f"[VIDEO] {job.assignment_id}: delivered but not recorded: {exc!r}")
        job.outcome = "delivered"
        job.url = url
        print(
            f"[VIDEO] Delivered assignment {job.assignment_id} (video {job.video_id}) to {job.clipper_id} "
            f"in {result.report.get('elapsed_s')} s, attempt {result.report.get('accepted_attempt')}"
        )
        if job.interaction is not None:
            try:
                mention = getattr(job.thread, "mention", "ton fil privé")
                await job.interaction.edit_original_response(content=f"✅ Ta vidéo est prête dans {mention}.")
            except (discord.HTTPException, ValueError):
                pass
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        try:
            await asyncio.to_thread(_housekeeping)
        except Exception as exc:
            print(f"[VIDEO] Housekeeping failed: {exc!r}")


def _housekeeping() -> None:
    get_store().housekeeping()
    video_hosting.cleanup_expired()


# ---------------------------------------------------------------------------
# Button
# ---------------------------------------------------------------------------

def availability_problem() -> str | None:
    if not _started or not get_queue().started:
        return "La génération vidéo démarre, réessaie dans une minute."
    if _selfcheck_problems:
        return "La génération vidéo est indisponible (outils vidéo manquants sur le serveur). Préviens l’équipe."
    if not video_hosting.hosting_ready():
        return "La génération vidéo est indisponible (lien de téléchargement non configuré). Préviens l’équipe."
    try:
        get_store().list_videos()
    except StoreUnavailable:
        return "La génération vidéo est indisponible (stockage illisible). Préviens l’équipe."
    return None


async def _reply(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


async def handle_video_click(interaction: discord.Interaction) -> None:
    from content_generator import _creator_gate, _note_in_thread, get_or_create_clips_thread

    if await _creator_gate(interaction):
        return
    if not isinstance(interaction.channel, discord.TextChannel) or not isinstance(interaction.user, discord.Member):
        await _reply(interaction, "Ce bouton fonctionne uniquement dans le salon du générateur.")
        return
    problem = availability_problem()
    if problem:
        await _reply(interaction, f"❌ {problem}")
        return

    member = interaction.user
    queue = get_queue()
    claim = queue.claim(member.id)
    if claim == "duplicate":
        ahead = queue.position(member.id)
        detail = f" {eta_message(ahead)}" if ahead is not None else ""
        await _reply(interaction, f"⏳ Ta vidéo est déjà en préparation, une seule demande à la fois.{detail}")
        return
    if claim == "full":
        await _reply(interaction, "⏳ La file vidéo est pleine. Réessaie dans quelques minutes.")
        return

    enqueued = False
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            thread = await get_or_create_clips_thread(interaction.channel, member, mode="video")
        except Exception as exc:
            print(f"[VIDEO] Clips thread failed for {member.id}: {exc!r}")
            await _reply(interaction, f"❌ Impossible d’ouvrir ton fil privé. {NOTHING_COUNTED}, réessaie.")
            return
        store = get_store()
        try:
            assignment = store.reserve(member.id, thread_id=getattr(thread, "id", None))
        except NoActiveVideo:
            await _reply(interaction, "Aucune vidéo n’est disponible pour le moment. L’équipe en ajoute bientôt.")
            return
        except AlreadyReserved:
            await _reply(interaction, "⏳ Ta vidéo est déjà en préparation, une seule demande à la fois.")
            return
        except StoreUnavailable as exc:
            print(f"[VIDEO] Store unavailable: {exc}")
            await _reply(interaction, "❌ La génération vidéo est indisponible (stockage illisible). Préviens l’équipe.")
            return
        job = VideoJob(
            assignment_id=assignment["id"],
            clipper_id=member.id,
            video_id=assignment["video_id"],
            seed=assignment["seed"],
            pitch_slot=assignment["pitch_slot"],
            member=member,
            thread=thread,
            interaction=interaction,
        )
        try:
            ahead = queue.put(job)
        except Exception:
            store.release(assignment["id"], "enqueue")
            raise
        enqueued = True
    finally:
        if not enqueued:
            queue.unclaim(member.id)

    await _note_in_thread(thread, f"{member.mention} lance **Vidéo**.")
    mention = getattr(thread, "mention", "ton fil privé")
    await _reply(interaction, f"🎞️ Demande reçue. Ta vidéo arrive dans {mention}. {eta_message(ahead)}")


# ---------------------------------------------------------------------------
# Staff commands
# ---------------------------------------------------------------------------

def clean_title(raw: str) -> str | None:
    text = "".join(ch for ch in raw if unicodedata.category(ch)[0] != "C")
    text = re.sub(r"\s+", " ", text).strip()
    return text if 1 <= len(text) <= TITLE_MAX else None


def attachment_problem(attachment: discord.Attachment) -> str | None:
    name = (attachment.filename or "").lower()
    content_type = (attachment.content_type or "").split(";")[0].strip().lower()
    if not (name.endswith(VIDEO_EXTENSIONS) or content_type.startswith("video/")):
        return "Envoie un fichier vidéo `.mp4`, `.mov` ou `.m4v`."
    if not attachment.size:
        return "Le fichier est vide."
    limit = max_upload_bytes()
    if attachment.size > limit:
        return f"La vidéo fait {attachment.size / 1e6:.0f} Mo, le maximum est {limit / 1024 / 1024:.0f} Mo."
    return None


def _extension(attachment: discord.Attachment) -> str:
    suffix = Path(attachment.filename or "").suffix.lower()
    return suffix if suffix in VIDEO_EXTENSIONS else ".mp4"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _non_persistent_note() -> str:
    store = get_store()
    return "" if store.persistent else "\n⚠️ Stockage NON persistant : les vidéos seront perdues au prochain déploiement."


@app_commands.command(name="add-video", description="Staff : ajoute une vidéo source pour le bouton Vidéo.")
@app_commands.describe(video="Vidéo verticale 9:16 avec son, 180 s maximum", titre="Nom interne (visible par le staff)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def add_video_command(interaction: discord.Interaction, video: discord.Attachment, titre: str) -> None:
    if not is_staff(interaction):
        await _reply(interaction, "⛔ Commande réservée au staff.")
        return
    title = clean_title(titre)
    if title is None:
        await _reply(interaction, f"Le titre doit faire entre 1 et {TITLE_MAX} caractères.")
        return
    problem = attachment_problem(video)
    if problem:
        await _reply(interaction, f"❌ {problem}")
        return
    if not media.ffprobe_bin():
        await _reply(interaction, "❌ ffprobe est absent du serveur, impossible de vérifier la vidéo.")
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        store = get_store()
        store.list_videos()
    except StoreUnavailable as exc:
        await _reply(interaction, f"❌ Stockage vidéo illisible : {safe_text(str(exc))}")
        return

    ext = _extension(video)
    incoming = store.sources_dir / f".incoming-{secrets.token_hex(8)}{ext}"
    final: Path | None = None
    try:
        await video.save(incoming)  # Discord CDN only: no URL is ever taken from the user
        size = incoming.stat().st_size
        if size > max_upload_bytes():
            await _reply(interaction, "❌ Fichier trop lourd.")
            return
        info = await asyncio.to_thread(lambda: media.source_info(media.probe(incoming)))
        sha = await asyncio.to_thread(_sha256, incoming)
        duplicate = store.find_active_by_sha(sha)
        if duplicate:
            await _reply(interaction, f"Cette vidéo est déjà active : **{safe_text(duplicate['title'])}** (`{duplicate['id']}`).")
            return
        video_id = secrets.token_hex(6)
        final = store.sources_dir / f"{video_id}{ext}"
        os.replace(incoming, final)
        record = store.add_video(
            source_file=final.name, title=title, sha256=sha, size_bytes=size,
            duration=info.duration, width=info.width, height=info.height,
            added_by=interaction.user.id, video_id=video_id,
        )
        final = None
        active = sum(1 for v in store.list_videos() if v.get("active"))
        await _reply(
            interaction,
            f"✅ Vidéo ajoutée : **{safe_text(record['title'])}** (`{record['id']}`), "
            f"{info.duration:.0f} s, {info.width}×{info.height}. "
            f"{active} vidéo{'s' if active > 1 else ''} active{'s' if active > 1 else ''}."
            + _non_persistent_note(),
        )
    except media.MediaError as exc:
        await _reply(interaction, f"❌ {safe_text(str(exc))}")
    except StoreUnavailable as exc:
        await _reply(interaction, f"❌ Stockage vidéo illisible : {safe_text(str(exc))}")
    except discord.HTTPException:
        await _reply(interaction, "❌ Téléchargement de la pièce jointe impossible, réessaie.")
    finally:
        incoming.unlink(missing_ok=True)
        if final is not None:
            final.unlink(missing_ok=True)


@app_commands.command(name="list-videos", description="Staff : liste les vidéos sources du bouton Vidéo.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def list_videos_command(interaction: discord.Interaction) -> None:
    if not is_staff(interaction):
        await _reply(interaction, "⛔ Commande réservée au staff.")
        return
    try:
        videos = get_store().list_videos()
    except StoreUnavailable as exc:
        await _reply(interaction, f"❌ Stockage vidéo illisible : {safe_text(str(exc))}")
        return
    if not videos:
        await _reply(interaction, "Aucune vidéo. Ajoute-en une avec `/add-video`." + _non_persistent_note())
        return
    active = sum(1 for v in videos if v.get("active"))
    lines = [f"**{active} active{'s' if active > 1 else ''} / {len(videos)}**" + _non_persistent_note()]
    ordered = sorted(videos, key=lambda v: (not v.get("active"), -v["added_at"]))
    for index, v in enumerate(ordered):
        line = (
            f"{'🟢' if v.get('active') else '⛔'} `{v['id']}` **{safe_text(v['title'])}** · "
            f"{v['duration']:.0f} s · {v.get('assigned_count', 0)} livraison(s)"
        )
        if sum(len(x) + 1 for x in lines) + len(line) > 1900:
            lines.append(f"… et {len(ordered) - index} autre(s).")
            break
        lines.append(line)
    await _reply(interaction, "\n".join(lines))


@app_commands.command(name="disable-video", description="Staff : retire une vidéo source de la rotation.")
@app_commands.describe(video="Identifiant de la vidéo (voir /list-videos)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def disable_video_command(interaction: discord.Interaction, video: str) -> None:
    if not is_staff(interaction):
        await _reply(interaction, "⛔ Commande réservée au staff.")
        return
    video_id = video.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{6,32}", video_id):
        await _reply(interaction, "Identifiant invalide. Copie-le depuis `/list-videos`.")
        return
    try:
        store = get_store()
        before = store.get_video(video_id)
        record = store.disable_video(video_id, interaction.user.id)
    except StoreUnavailable as exc:
        await _reply(interaction, f"❌ Stockage vidéo illisible : {safe_text(str(exc))}")
        return
    if record is None:
        await _reply(interaction, f"Aucune vidéo avec l’identifiant `{video_id}`.")
        return
    if before is not None and not before.get("active"):
        await _reply(interaction, f"La vidéo **{safe_text(record['title'])}** (`{video_id}`) est déjà désactivée.")
        return
    await _reply(
        interaction,
        f"⛔ Vidéo désactivée : **{safe_text(record['title'])}** (`{video_id}`). Elle ne sera plus attribuée. "
        "Les demandes déjà en file pour cette vidéo seront annulées sans être comptées.",
    )


@disable_video_command.autocomplete("video")
async def _disable_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if not is_staff(interaction):
        return []
    try:
        videos = get_store().list_videos()
    except StoreUnavailable:
        return []
    needle = current.lower()
    choices = []
    for v in videos:
        if v.get("active") and (needle in v["id"] or needle in v["title"].lower()):
            choices.append(app_commands.Choice(name=f"{v['title'][:80]} ({v['id']})", value=v["id"]))
    return choices[:25]


def register(bot: discord.Client) -> None:
    bot.tree.add_command(add_video_command)
    bot.tree.add_command(list_videos_command)
    bot.tree.add_command(disable_video_command)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def start(bot: discord.Client | None = None) -> None:
    """Idempotent (on_ready can fire again after a reconnect)."""
    global _started, _selfcheck_problems
    if _started:
        return
    _started = True
    _selfcheck_problems = await asyncio.to_thread(media.selfcheck)
    if _selfcheck_problems:
        print("[VIDEO] Selfcheck FAILED, the Vidéo button is disabled: " + "; ".join(_selfcheck_problems))
    else:
        print("[VIDEO] Selfcheck OK (ffmpeg + rubberband + libx264, ffprobe, fpcalc)")
    try:
        store = get_store()
        interrupted = await asyncio.to_thread(store.recover_after_restart)
        await asyncio.to_thread(_housekeeping)
        print(f"[VIDEO] Store at {store.root} ({'persistent' if store.persistent else 'NOT persistent'})")
    except StoreUnavailable as exc:
        print(f"[VIDEO] Store unavailable: {exc}")
        interrupted = []
    if not video_hosting.hosting_ready():
        print("[VIDEO] Hosting not ready: set PORT and PUBLIC_BASE_URL (or RAILWAY_PUBLIC_DOMAIN)")
    get_queue().start()
    if bot is not None:
        for row in interrupted:
            await _notify_interrupted(bot, row)


async def _notify_interrupted(bot: discord.Client, row: dict) -> None:
    thread_id = row.get("thread_id")
    if not thread_id:
        return
    try:
        thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
        await thread.send(
            f"<@{row['clipper_id']}> ⚠️ Ta demande vidéo a été interrompue par un redémarrage du bot. "
            f"{NOTHING_COUNTED}, reclique sur **Vidéo**.",
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
    except (discord.HTTPException, AttributeError) as exc:
        print(f"[VIDEO] Could not notify interrupted request {row.get('id')}: {exc}")


async def stop() -> None:
    global _started
    if _queue is not None:
        await _queue.stop()
    _started = False
