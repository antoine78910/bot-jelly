"""Offline Discord doubles for the Vidéo tests and scripts/preview_videos.py.

They pass the isinstance checks the bot makes (TextChannel, Member, Attachment)
and record what would have been sent. Nothing here touches the network.
"""

from __future__ import annotations

import itertools
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import discord

_ids = itertools.count(1_700_000_000_000_000_000)


def next_id() -> int:
    return next(_ids)


class FakeThread:
    def __init__(self, name: str, guild: "FakeGuild", parent_id: int) -> None:
        self.id = next_id()
        self.name = name
        self.guild = guild
        self.parent_id = parent_id
        self.archived = False
        self.mention = f"<#{self.id}>"
        self.sent: list[dict] = []
        self.members: list[int] = []
        self.fail_sends = 0  # next N sends raise HTTPException

    async def send(self, content=None, **kwargs):
        if self.fail_sends:
            self.fail_sends -= 1
            raise http_exception(403, "Missing Permissions")
        self.sent.append({"content": content, **kwargs})
        return MagicMock(id=next_id())

    async def add_user(self, user) -> None:
        self.members.append(user.id)

    async def edit(self, **kwargs) -> None:
        self.archived = kwargs.get("archived", self.archived)


class FakeGuild:
    def __init__(self) -> None:
        self.id = next_id()
        self.name = "Test guild"
        self.me = MagicMock(id=next_id())
        self.me.top_role.name = "Bot"
        self._members: dict[int, object] = {}

    def get_role(self, role_id):
        return None

    def get_member(self, user_id):
        return self._members.get(user_id) or MagicMock(id=user_id)

    async def fetch_member(self, user_id):
        return self.get_member(user_id)

    async def active_threads(self):
        return []


class _EmptyAsyncIter:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def make_channel(guild: FakeGuild | None = None, *, fail_create: bool = False):
    guild = guild or FakeGuild()
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = next_id()
    channel.guild = guild
    channel.threads = []
    channel.mention = f"<#{channel.id}>"
    channel.created: list[FakeThread] = []
    channel.archived_threads = lambda **kwargs: _EmptyAsyncIter()

    async def create_thread(*, name, **kwargs):
        if fail_create:
            raise http_exception(403, "Missing Permissions")
        thread = FakeThread(name, guild, channel.id)
        channel.threads.append(thread)
        channel.created.append(thread)
        return thread

    channel.create_thread = create_thread
    return channel


def make_member(guild: FakeGuild, *, name: str, roles: tuple[int, ...] = (), admin: bool = False):
    member = MagicMock(spec=discord.Member)
    member.id = next_id()
    member.name = name
    member.display_name = name
    member.bot = False
    member.mention = f"<@{member.id}>"
    member.guild = guild
    member.guild_permissions = discord.Permissions(administrator=admin)
    member.get_role = lambda role_id: MagicMock(id=role_id) if role_id in roles else None
    guild._members[member.id] = member
    return member


class FakeResponse:
    def __init__(self) -> None:
        self._done = False
        self.messages: list[dict] = []
        self.modal = None
        self.deferred = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, **kwargs):
        self._done = True
        self.messages.append({"content": content, **kwargs})

    async def defer(self, **kwargs):
        self._done = True
        self.deferred = True

    async def send_modal(self, modal):
        self._done = True
        self.modal = modal


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class FakeInteraction:
    def __init__(self, user, channel=None) -> None:
        self.id = next_id()
        self.user = user
        self.channel = channel
        self.guild = getattr(channel, "guild", None)
        self.client = None
        self.data = {}
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.edits: list[dict] = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)

    async def delete_original_response(self):
        pass

    def texts(self) -> list[str]:
        """Every text this interaction showed to the user, in order."""
        out = [m.get("content") or "" for m in self.response.messages]
        out += [m.get("content") or "" for m in self.followup.messages]
        out += [e.get("content") or "" for e in self.edits]
        return out


def make_attachment(source: Path, *, filename: str | None = None, content_type: str = "video/quicktime", size: int | None = None):
    attachment = MagicMock(spec=discord.Attachment)
    attachment.filename = filename or source.name
    attachment.content_type = content_type
    attachment.size = source.stat().st_size if size is None else size
    attachment.url = "https://cdn.discordapp.com/attachments/fake"

    async def save(dest, **kwargs):
        shutil.copyfile(source, dest)
        return attachment.size

    attachment.save = save
    return attachment


def http_exception(status: int, text: str) -> discord.HTTPException:
    response = MagicMock(status=status, reason=text)
    return discord.HTTPException(response, text)


def view_urls(view) -> list[str]:
    return [item.url for item in getattr(view, "children", []) if getattr(item, "url", None)]
