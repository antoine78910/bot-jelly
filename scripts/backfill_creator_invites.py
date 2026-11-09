"""One-shot: DM already-approved creators their Creator platform link.

Dry-run by default. Nothing calls the Creator API and no DM is sent unless
--send is passed.

The registration log channel is the source of truth (the JSON store has no
Railway volume and is rebuilt from that channel). --send always re-reads the
log. A dry-run uses the store when it already contains approved rows, and
otherwise reads the same channel over HTTP, without opening a gateway
session, so the bot that is already online stays connected.

Idempotency, in order:
  - creator_invite_sent on the registration (store, or the log embed notice
    "Lien Creator envoyé.")
  - data/creator-invite-backfill.json next to the store (or CREATOR_INVITE_STATE_PATH)

A successful send is recorded before the next account is processed. Closed
DMs (Discord 403 Forbidden) are reported and left unmarked so a later run
can retry them. There is a pause between accounts.

Usage:
  python scripts/backfill_creator_invites.py
  python scripts/backfill_creator_invites.py --only 123456789012345678
  python scripts/backfill_creator_invites.py --limit 10
  python scripts/backfill_creator_invites.py --from-log
  python scripts/backfill_creator_invites.py --send --only 123456789012345678
  python scripts/backfill_creator_invites.py --send --limit 5

From a machine with the Railway CLI, this repo checked out, and the bot
service linked (runs locally with the service env, still no gateway):

  railway run python scripts/backfill_creator_invites.py
  railway run python scripts/backfill_creator_invites.py --send --limit 5

--send also needs CREATOR_BOT_API_BASE_URL and CREATOR_BOT_INVITE_SECRET.
A dry-run does not. Do not set those on production until the Creator API
is deployed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import registration
from creator_invite import (
    CreatorInviteError,
    already_sent,
    backfill_dm,
    format_dry_run,
    load_state,
    only_status_message,
    plan_backfill,
    request_creator_invite,
    save_state,
    state_path_for,
)

DM_PAUSE_SECONDS = 2.0
INVITE_SENT_FALLBACK = "Lien Creator envoyé à"


def _token() -> str:
    return os.getenv("DISCORD_TOKEN", "").strip().strip('"').strip("'")


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _load_store() -> None:
    registration._store_loaded = False
    registration._registrations = {}
    registration._load_store()


def _approved_count() -> int:
    return sum(1 for record in registration._registrations.values() if record.get("status") == "approved")


async def _open_client(token: str) -> discord.Client:
    client = discord.Client(intents=discord.Intents.none())
    try:
        await client.login(token)
    except Exception:
        await client.close()
        raise
    return client


async def _read_log(client: discord.Client) -> str:
    """Merge the log channel onto the store. Rows absent from the log are dropped."""
    channel_id = registration._log_channel_id()
    if not channel_id:
        raise RuntimeError("Salon de logs d’inscription non configuré.")
    channel = await client.fetch_channel(channel_id)
    previous = dict(registration._registrations)
    registration._registrations = {}
    checked = 0
    marked_sent: set[str] = set()
    bot_id = client.user.id if client.user else None
    async for message in channel.history(limit=None, oldest_first=True):
        checked += 1
        if checked % 500 == 0:
            print(f"Lecture du salon de logs : {checked} messages...", file=sys.stderr)
        if bot_id is not None and message.author.id != bot_id:
            continue
        marker = registration.invite_sent_discord_id(message.content or "")
        if marker:
            marked_sent.add(marker)
        parsed = registration._record_from_message(message)
        if parsed is None:
            continue
        previous_row = previous.get(parsed["discord_id"], {})
        if previous_row.get("creator_invite_sent") and "creator_invite_sent" not in parsed:
            parsed = {**parsed, "creator_invite_sent": True}
        registration.merge_log_record(parsed)
    for discord_id in marked_sent:
        record = registration._registrations.get(discord_id)
        if record:
            record["creator_invite_sent"] = True
    approved = _approved_count()
    return (
        f"salon de logs {channel_id} "
        f"({checked} message(s) lus, {approved} approuvée(s))"
    )


def _mark_sent(state: dict, record: dict, result, *, state_path: Path) -> None:
    discord_id = str(record["discord_id"])
    record["creator_invite_sent"] = True
    record["creator_invite_sent_at"] = registration._now_iso()
    record["dm_sent"] = True
    record.pop("creator_invite_error", None)
    if result.creator_id:
        record["creator_platform_id"] = result.creator_id
    registration._registrations[discord_id] = record
    registration._save_store()
    state.setdefault("sent", {})[discord_id] = {
        "sent_at": record["creator_invite_sent_at"],
        "username": record.get("discord_username") or "",
        "creator_id": result.creator_id,
        "already_accepted": result.already_accepted,
    }
    save_state(state_path, state)


async def _note_on_log(client: discord.Client, record: dict) -> None:
    """Make the send survive a store wipe by editing the registration embed."""
    channel_id = record.get("channel_id") or registration._log_channel_id()
    message_id = record.get("message_id")
    if not channel_id or not message_id:
        await _fallback_log_line(client, record, reason="message de log introuvable")
        return
    try:
        channel = await client.fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
        await message.edit(embed=registration.registration_embed(record))
    except discord.HTTPException as exc:
        print(
            f"  Embed de log non mis à jour (HTTP {exc.status}). "
            "Un message de secours est posté pour que le prochain passage saute ce compte.",
            file=sys.stderr,
        )
        await _fallback_log_line(client, record, reason=f"HTTP {exc.status}")


async def _fallback_log_line(client: discord.Client, record: dict, *, reason: str) -> None:
    channel_id = registration._log_channel_id()
    if not channel_id:
        print(f"  Marqueur de log non posté ({reason}), salon inconnu.", file=sys.stderr)
        return
    username = str(record.get("discord_username") or "").strip().lstrip("@")
    who = f" (@{username})" if username else ""
    text = f"{INVITE_SENT_FALLBACK} <@{record['discord_id']}>{who}."
    try:
        channel = await client.fetch_channel(channel_id)
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as exc:
        print(f"  Marqueur de log non posté : HTTP {exc.status}.", file=sys.stderr)


async def _deliver_one(client: discord.Client, record: dict):
    display_name = str(record.get("discord_username") or "").strip()
    user = None
    try:
        user = await client.fetch_user(int(record["discord_id"]))
        display_name = user.global_name or user.display_name or user.name or display_name
    except discord.HTTPException:
        user = None
    result = await request_creator_invite(record, display_name=display_name or None)
    if user is None:
        user = await client.fetch_user(int(record["discord_id"]))
    text = backfill_dm(result.invite_url, already_accepted=result.already_accepted)
    await user.send(text)
    return result


async def _send_all(
    client: discord.Client,
    targets: list[dict],
    state: dict,
    *,
    state_path: Path,
    deliver=None,
) -> int:
    deliver = deliver or _deliver_one
    failures = 0
    for index, record in enumerate(targets):
        discord_id = str(record.get("discord_id"))
        username = str(record.get("discord_username") or "").strip().lstrip("@")
        label = f"{discord_id}  @{username}" if username else discord_id
        if already_sent(record, state):
            print(f"Ignore (déjà traité) : {label}")
        else:
            print(f"Envoi : {label}")
            try:
                result = await deliver(client, record)
            except CreatorInviteError as exc:
                failures += 1
                print(f"  API en échec, DM non envoyé : {exc.public_message()}")
            except discord.Forbidden:
                failures += 1
                print("  DM fermés (Forbidden). Le compte n’est pas marqué, un prochain passage pourra réessayer.")
            except discord.NotFound:
                failures += 1
                print("  Utilisateur Discord introuvable. Le compte n’est pas marqué.")
            except discord.HTTPException as exc:
                failures += 1
                if exc.status == 403:
                    print("  DM fermés (Forbidden). Le compte n’est pas marqué, un prochain passage pourra réessayer.")
                else:
                    print(f"  DM non envoyé : HTTP {exc.status}. Le compte n’est pas marqué.")
            else:
                _mark_sent(state, record, result, state_path=state_path)
                await _note_on_log(client, record)
                accepted = "oui" if result.already_accepted else "non"
                print(f"  DM envoyé. Déjà accepté : {accepted}.")
        if index + 1 < len(targets):
            await asyncio.sleep(DM_PAUSE_SECONDS)
    print(f"Terminé. {len(targets) - failures} envoyé(s), {failures} en échec.")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Envoie le lien d’espace Creator aux inscriptions déjà approuvées."
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Appelle l’API Creator puis envoie les DM. Sans ce drapeau, dry-run.",
    )
    parser.add_argument(
        "--only",
        metavar="DISCORD_ID",
        help="Ne traite que ce compte Discord.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Nombre maximum de comptes à traiter, après ceux déjà marqués.",
    )
    parser.add_argument(
        "--from-log",
        action="store_true",
        help="Reconstruit les inscriptions depuis le salon de logs, même si le store contient des approuvées.",
    )
    return parser


async def async_main(args: argparse.Namespace) -> int:
    if args.only and not str(args.only).isdigit():
        print("--only attend un identifiant Discord numérique.", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 1:
        print("--limit doit être supérieur à 0.", file=sys.stderr)
        return 2

    _load_store()
    store_path = registration._store_path()
    state_path = state_path_for(store_path)
    use_log = args.send or args.from_log or _approved_count() == 0
    token = _token()
    client = None
    try:
        if use_log:
            if not token:
                print(
                    "Le salon de logs est nécessaire (store sans approuvée, --from-log ou --send) "
                    "et DISCORD_TOKEN est absent.\n"
                    "Sur Railway, depuis ce dépôt : "
                    "railway run python scripts/backfill_creator_invites.py",
                    file=sys.stderr,
                )
                return 2
            try:
                client = await _open_client(token)
            except discord.LoginFailure:
                print("Connexion Discord refusée. Vérifie DISCORD_TOKEN.", file=sys.stderr)
                return 2
            try:
                source = await _read_log(client)
            except discord.HTTPException as exc:
                print(f"Lecture du salon de logs impossible : HTTP {exc.status}.", file=sys.stderr)
                return 2
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 2
        else:
            source = f"store {store_path} ({_approved_count()} approuvée(s) dans le fichier)"

        records = list(registration._registrations.values())
        if args.only:
            reason = only_status_message(records, str(args.only))
            if reason:
                print(reason)
                return 1
        state = load_state(state_path)
        targets, skipped = plan_backfill(records, state, only=args.only, limit=args.limit)
        if not args.send:
            print(format_dry_run(source=source, targets=targets, skipped=skipped), end="")
            return 0
        if client is None:
            print("Client Discord indisponible.", file=sys.stderr)
            return 2
        if not targets:
            print("Rien à envoyer. Les comptes déjà marqués sont ignorés.")
            return 0
        return await _send_all(client, targets, state, state_path=state_path)
    finally:
        if client is not None:
            await client.close()


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    load_dotenv(ROOT / ".env", override=True)
    args = build_parser().parse_args(argv)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
