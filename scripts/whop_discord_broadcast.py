"""
One-off Whop support-chat broadcast — invite members to the Jelly Discord.

Safe to delete after use. Does not touch the Discord bot.

Setup (.env in project root):
  WHOP_API_KEY=...          # Company API key (Whop dashboard → Developer → API keys)
  WHOP_COMPANY_ID=biz_...   # Your Whop company ID

Required API key permissions:
  - member:basic:read
  - support_chat:create
  - support_chat:message:create

Usage:
  python scripts/whop_discord_broadcast.py --dry-run
  python scripts/whop_discord_broadcast.py --username antocodes
  python scripts/whop_discord_broadcast.py --include-left --dry-run
  python scripts/whop_discord_broadcast.py --limit 3
  python scripts/whop_discord_broadcast.py --yes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from whop_api import WhopClient, find_actor_user_id, member_user_id

load_dotenv(override=True)

DISCORD_INVITE = "https://discord.gg/SqSSp8sMq7"
STATE_PATH = Path(__file__).parent / "whop_broadcast_state.json"

BROADCAST_MESSAGE = f"""Hey! 👋

We just launched the **Jelly Clipping Discord** — join here:
{DISCORD_INVITE}

Inside you'll get everything you need to start earning:
- Click **Generate Content** to get ready-to-post clips
- Post them on Instagram — that's it

No complicated setup. Just generate, post, and get paid.

See you there! 🚀"""


def _member_username(member: dict) -> str:
    user = member.get("user") or {}
    return str(user.get("username") or "").lower()


def _is_broadcast_target(member: dict, *, include_left: bool, include_admin: bool) -> bool:
    user_id = member_user_id(member)
    if not user_id:
        return False
    if member.get("access_level") == "admin" and not include_admin:
        return False
    status = member.get("status")
    if status == "joined":
        return True
    if include_left and status == "left":
        return True
    return False


def _find_member_by_username(members: list[dict], username: str) -> dict | None:
    needle = username.lstrip("@").lower()
    for member in members:
        if _member_username(member) == needle:
            return member
    return None


def _send_to_member(
    client: WhopClient,
    company_id: str,
    actor_user_id: str,
    member: dict,
    *,
    sent_ids: set[str],
    save_state: bool,
) -> None:
    user = member.get("user") or {}
    user_id = str(user["id"])

    client.send_support_message(company_id, actor_user_id, user_id, BROADCAST_MESSAGE)
    if save_state:
        sent_ids.add(user_id)
        _save_state(sent_ids)


def _load_state() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    with STATE_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    return set(data.get("sent_user_ids") or [])


def _save_state(sent_user_ids: set[str]) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump({"sent_user_ids": sorted(sent_user_ids)}, f, indent=2)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Broadcast Discord invite via Whop support chat.")
    parser.add_argument("--username", default="", help="Send to one Whop username only (e.g. antocodes).")
    parser.add_argument("--include-left", action="store_true", help="Also message members with status=left.")
    parser.add_argument("--include-admin", action="store_true", help="Include admin accounts (usually skipped).")
    parser.add_argument("--dry-run", action="store_true", help="Preview targets without sending.")
    parser.add_argument("--limit", type=int, default=0, help="Max members to process (0 = all).")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between sends.")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    parser.add_argument("--force", action="store_true", help="Send even if user was already messaged.")
    args = parser.parse_args()

    api_key = os.getenv("WHOP_API_KEY", "").strip()
    company_id = os.getenv("WHOP_COMPANY_ID", "").strip()
    if not api_key:
        print("Missing WHOP_API_KEY in .env", file=sys.stderr)
        return 1
    if not company_id.startswith("biz_"):
        print("Missing or invalid WHOP_COMPANY_ID in .env (expected biz_...)", file=sys.stderr)
        return 1

    client = WhopClient(api_key)
    print(f"Fetching members for {company_id}...")
    all_members = client.list_members(company_id)
    actor_user_id = find_actor_user_id(
        all_members,
        os.getenv("WHOP_ACTOR_USER_ID", ""),
    )
    actor = next(
        (m for m in all_members if member_user_id(m) == actor_user_id),
        None,
    )
    actor_name = ""
    if actor:
        actor_name = (actor.get("user") or {}).get("username") or actor_user_id
    print(f"Sending as Whop user: @{actor_name} ({actor_user_id})")

    if args.username:
        member = _find_member_by_username(all_members, args.username)
        if member is None:
            print(f"User @{args.username.lstrip('@')} not found in member list.", file=sys.stderr)
            return 1
        targets = [member]
        print(f"Single-user test: @{_member_username(member)} ({member.get('status')})")
    else:
        members = [
            m
            for m in all_members
            if _is_broadcast_target(
                m,
                include_left=args.include_left,
                include_admin=args.include_admin,
            )
        ]
        label = "joined + left" if args.include_left else "joined"
        print(f"Found {len(members)} target member(s) ({label}).")

        already_sent = _load_state()
        targets = []
        for member in members:
            user_id = member_user_id(member)
            assert user_id
            if not args.force and user_id in already_sent:
                continue
            targets.append(member)

        if args.limit > 0:
            targets = targets[: args.limit]

    if not targets:
        print("No members to message (all done or filtered out).")
        return 0

    print(f"\nTargets ({len(targets)}):")
    for member in targets[:10]:
        user = member.get("user") or {}
        label = user.get("username") or user.get("name") or user.get("id")
        print(f"  - {label} ({user.get('id')})")
    if len(targets) > 10:
        print(f"  ... and {len(targets) - 10} more")

    print("\nMessage preview:\n")
    print(BROADCAST_MESSAGE)
    print()

    if args.dry_run:
        print("Dry run — nothing sent.")
        return 0

    if not args.yes:
        answer = input(f"Send to {len(targets)} member(s)? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 0

    sent_ids = set(_load_state())
    ok = failed = 0
    save_state = not args.username

    for index, member in enumerate(targets, start=1):
        user = member.get("user") or {}
        user_id = str(user["id"])
        label = user.get("username") or user.get("name") or user_id

        try:
            _send_to_member(
                client,
                company_id,
                actor_user_id,
                member,
                sent_ids=sent_ids,
                save_state=save_state,
            )
            ok += 1
            print(f"[{index}/{len(targets)}] OK  {label}")
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(targets)}] FAIL {label}: {exc}", file=sys.stderr)

        if index < len(targets) and args.delay > 0:
            time.sleep(args.delay)

    print(f"\nDone: {ok} sent, {failed} failed.")
    if save_state:
        print(f"State saved to {STATE_PATH}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
