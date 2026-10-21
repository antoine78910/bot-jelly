"""Who counts as staff: owners (notify_user_ids), the staff role, or server admins."""

from __future__ import annotations

import os

import discord

from notify_roles import notify_role_id, notify_user_ids


def staff_role_id() -> int | None:
    env = os.getenv("STAFF_ROLE_ID", "").strip()
    if env.isdigit():
        return int(env)
    return notify_role_id()


def is_staff_member(user: discord.abc.User | None) -> bool:
    if user is None or user.bot:
        return False
    if user.id in notify_user_ids():
        return True
    if not isinstance(user, discord.Member):
        return False
    perms = user.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    role_id = staff_role_id()
    return bool(role_id and user.get_role(role_id))


def is_staff(interaction: discord.Interaction) -> bool:
    return is_staff_member(interaction.user)
