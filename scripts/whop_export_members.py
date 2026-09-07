"""
Export Whop members (with emails) for review before any broadcast.

Requires member:email:read on your Whop API key (in addition to member:basic:read).

Usage:
  python scripts/whop_export_members.py
  python scripts/whop_export_members.py --csv scripts/whop_members.csv
  python scripts/whop_export_members.py --show-emails
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from whop_api import WhopClient, member_email, member_user_id

load_dotenv(override=True)

DEFAULT_CSV = Path(__file__).parent / "whop_members_export.csv"
DEFAULT_JSON = Path(__file__).parent / "whop_members_export.json"
DEFAULT_EMAILS_CSV = Path(__file__).parent / "whop_emails_all.csv"


def _is_export_member(member: dict, *, include_left: bool, include_admin: bool) -> bool:
    if not member_user_id(member):
        return False
    if member.get("access_level") == "admin" and not include_admin:
        return False
    status = member.get("status")
    if status == "joined":
        return True
    if include_left and status == "left":
        return True
    return False


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        masked_local = local[0] + "*"
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked_local}@{domain}"


def _row(member: dict) -> dict:
    user = member.get("user") or {}
    return {
        "user_id": member_user_id(member) or "",
        "username": user.get("username") or "",
        "name": user.get("name") or "",
        "email": member_email(member) or "",
        "status": member.get("status") or "",
        "access_level": member.get("access_level") or "",
        "most_recent_action": member.get("most_recent_action") or "",
        "joined_at": member.get("joined_at") or "",
        "usd_total_spent": member.get("usd_total_spent"),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Export Whop member emails and stats.")
    parser.add_argument("--include-left", action="store_true", help="Include members with status=left.")
    parser.add_argument("--include-admin", action="store_true", help="Include admin accounts.")
    parser.add_argument("--emails-csv", nargs="?", const=str(DEFAULT_EMAILS_CSV), default=None,
                        help=f"Write email-only CSV (default: {DEFAULT_EMAILS_CSV.name})")
    parser.add_argument("--csv", nargs="?", const=str(DEFAULT_CSV), default=None,
                        help=f"Write CSV export (default: {DEFAULT_CSV.name})")
    parser.add_argument("--json", nargs="?", const=str(DEFAULT_JSON), default=None,
                        help=f"Write JSON export (default: {DEFAULT_JSON.name})")
    parser.add_argument("--show-emails", action="store_true",
                        help="Print full emails in terminal (default: masked)")
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
    print(f"Fetching members for {company_id}...\n")

    try:
        all_members = client.list_members(company_id)
    except RuntimeError as exc:
        if "403" in str(exc) or "401" in str(exc):
            print(str(exc), file=sys.stderr)
            print(
                "\nCheck your API key permissions. Email export needs:\n"
                "  - member:basic:read\n"
                "  - member:email:read",
                file=sys.stderr,
            )
        else:
            print(str(exc), file=sys.stderr)
        return 1

    members = [
        m
        for m in all_members
        if _is_export_member(
            m,
            include_left=args.include_left,
            include_admin=args.include_admin,
        )
    ]
    rows = [_row(m) for m in members]
    with_email = [r for r in rows if r["email"]]
    without_email = [r for r in rows if not r["email"]]

    scope = "joined + left" if args.include_left else "joined"
    if args.include_admin:
        scope += " (+ admin)"

    status_counts = Counter(m.get("status") for m in all_members)
    access_counts = Counter(m.get("access_level") for m in all_members)

    print("=== Whop member summary ===")
    print(f"Company ID:        {company_id}")
    print(f"Total members:     {len(all_members)}")
    print(f"Exported users:    {len(members)}  ({scope}, with email field)")
    print(f"With email:        {len(with_email)}")
    print(f"Missing email:     {len(without_email)}")
    print()
    print("By status:")
    for key, count in sorted(status_counts.items(), key=lambda item: (-item[1], str(item[0]))):
        print(f"  {key or 'unknown'}: {count}")
    print()
    print("By access level:")
    for key, count in sorted(access_counts.items(), key=lambda item: (-item[1], str(item[0]))):
        print(f"  {key or 'unknown'}: {count}")

    preview = with_email[:15]
    if preview:
        print(f"\nSample emails ({len(preview)} of {len(with_email)}):")
        for row in preview:
            email = row["email"] if args.show_emails else _mask_email(row["email"])
            label = row["username"] or row["name"] or row["user_id"]
            print(f"  - {label}: {email}")

    if args.csv is not None:
        csv_path = Path(args.csv)
        fieldnames = list(rows[0].keys()) if rows else [
            "user_id", "username", "name", "email", "status",
            "access_level", "most_recent_action", "joined_at", "usd_total_spent",
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nCSV saved: {csv_path.resolve()}")

    if args.json is not None:
        json_path = Path(args.json)
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
        print(f"JSON saved: {json_path.resolve()}")

    if args.emails_csv is not None:
        emails_path = Path(args.emails_csv)
        with emails_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["email", "username", "name", "status", "user_id"])
            writer.writeheader()
            for row in with_email:
                writer.writerow(
                    {
                        "email": row["email"],
                        "username": row["username"],
                        "name": row["name"],
                        "status": row["status"],
                        "user_id": row["user_id"],
                    }
                )
        print(f"Emails CSV saved: {emails_path.resolve()} ({len(with_email)} rows)")

    print("\nNext step: review the export, then send email via Resend/Brevo or use Whop support chat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
