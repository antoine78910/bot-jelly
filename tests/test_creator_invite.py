"""Creator invite link: API client, non-blocking approval, and backfill dry-run."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

import registration
from creator_invite import (
    DRY_RUN_INVITE_URL,
    CreatorInviteError,
    InviteResult,
    TIMEOUT_SECONDS,
    backfill_dm,
    format_dry_run,
    invite_failure_message,
    invite_paragraph,
    invite_payload,
    load_state,
    plan_backfill,
    request_creator_invite,
    save_state,
)

LONG_DASHES = ("—", "–", "―")
SECRET = "SUPERSECRETVALUE99"
INVITE_URL = "https://creator.example/invite/abc"
LOGIN_URL = "https://creator.example/login"


def _load_backfill():
    path = Path(__file__).resolve().parents[1] / "scripts" / "backfill_creator_invites.py"
    spec = importlib.util.spec_from_file_location("backfill_creator_invites", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backfill = _load_backfill()


def _assert_no_long_dashes(testcase, text):
    for dash in LONG_DASHES:
        testcase.assertNotIn(dash, text)


class _HttpResponse:
    def __init__(self, status=403, reason="Forbidden"):
        self.status = status
        self.reason = reason


class FakeResponse:
    def __init__(self, status, body=None):
        self.status = status
        self.body = {} if body is None else body

    async def json(self, content_type=None):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.timeout = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class FakeUser:
    def __init__(self, user_id, name, global_name=None, fail=None):
        self.id = user_id
        self.name = name
        self.global_name = global_name
        self.display_name = global_name or name
        self.sent = []
        self.fail = fail

    async def send(self, content):
        if self.fail:
            raise self.fail
        self.sent.append(content)


class FakeClient:
    def __init__(self, user, channel):
        self.user = user
        self.channel = channel

    async def fetch_user(self, user_id):
        return self.user

    def get_channel(self, channel_id):
        return self.channel


class FakeResponseInteraction:
    def __init__(self):
        self.deferred = False

    async def defer(self, **kwargs):
        self.deferred = True

    async def send_message(self, content=None, **kwargs):
        self.sent = content


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append(content)


class FakeMember:
    def __init__(self, display_name):
        self.display_name = display_name


class FakeGuild:
    def __init__(self, display_name):
        self.member = FakeMember(display_name)

    def get_member(self, user_id):
        return self.member


def _record(**overrides):
    record = {
        "discord_id": "42",
        "discord_username": "alice",
        "country": "FR",
        "instagram": "@alice.ig",
        "tiktok": registration.NOT_YET,
        "phone": "+33 6 00 00 00 00",
        "payout_method": "PayPal",
        "payout_details": "paypal: secret-pay@example.com",
        "status": "pending",
        "applied_at": "2026-11-01T00:00:00+00:00",
    }
    record.update(overrides)
    return record


class CreatorInviteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmp.name) / "registrations.json"
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "CREATOR_STORE_PATH",
                "CREATOR_INVITE_STATE_PATH",
                "CREATOR_BOT_API_BASE_URL",
                "CREATOR_BOT_INVITE_SECRET",
                "REGISTRATION_LOG_CHANNEL_ID",
                "DISCORD_TOKEN",
            )
        }
        os.environ["CREATOR_STORE_PATH"] = str(self.store_path)
        os.environ["CREATOR_INVITE_STATE_PATH"] = str(Path(self.tmp.name) / "state.json")
        os.environ["REGISTRATION_LOG_CHANNEL_ID"] = "555"
        os.environ.pop("CREATOR_BOT_API_BASE_URL", None)
        os.environ.pop("CREATOR_BOT_INVITE_SECRET", None)
        os.environ.pop("DISCORD_TOKEN", None)
        registration._registrations = {}
        registration._store_loaded = False

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        registration._registrations = {}
        registration._store_loaded = False
        self.tmp.cleanup()

    def _session(self, responses):
        session = FakeSession(responses)

        def factory(*args, **kwargs):
            session.timeout = kwargs.get("timeout")
            return session

        return session, factory


class PayloadTests(CreatorInviteTestCase):
    def test_payload_uses_form_handles_and_skips_payment_data(self):
        payload = invite_payload(_record(), display_name="Alice N.")
        self.assertEqual(
            payload,
            {
                "discordId": "42",
                "discordUsername": "alice",
                "displayName": "Alice N.",
                "instagram": "alice.ig",
            },
        )
        self.assertNotIn("tiktok", payload)
        self.assertNotIn("email", payload)
        self.assertNotIn("phone", payload)
        blob = json.dumps(payload)
        self.assertNotIn("secret-pay@example.com", blob)
        self.assertNotIn("+33", blob)

    def test_explicit_email_is_forwarded_but_placeholders_are_not(self):
        payload = invite_payload(
            _record(email="alice@example.com", instagram="—", tiktok="  @tok  "),
        )
        self.assertEqual(payload["email"], "alice@example.com")
        self.assertEqual(payload["tiktok"], "tok")
        self.assertNotIn("instagram", payload)
        self.assertEqual(payload["displayName"], "alice")


class ApiClientAsyncTests(CreatorInviteTestCase, unittest.IsolatedAsyncioTestCase):
    async def _request(self, responses):
        os.environ["CREATOR_BOT_API_BASE_URL"] = "https://creator.example/"
        os.environ["CREATOR_BOT_INVITE_SECRET"] = SECRET
        session, factory = self._session(responses)
        with (
            patch("creator_invite.aiohttp.ClientSession", factory),
            patch("creator_invite.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            try:
                result = await request_creator_invite(_record(), display_name="Alice")
            except CreatorInviteError as exc:
                return session, exc, sleep
            return session, result, sleep

    async def test_success_sends_the_secret_header_and_parses_the_link(self):
        session, result, _sleep = await self._request(
            [FakeResponse(200, {"inviteUrl": INVITE_URL, "creatorId": "cr_1", "alreadyAccepted": False})]
        )
        self.assertIsInstance(result, InviteResult)
        self.assertEqual(result.invite_url, INVITE_URL)
        self.assertFalse(result.already_accepted)
        call = session.calls[0]
        self.assertEqual(call["url"], "https://creator.example/api/v1/internal/discord-creator-invite")
        self.assertEqual(call["headers"]["x-creator-bot-invite-secret"], SECRET)
        self.assertEqual(call["json"]["instagram"], "alice.ig")
        self.assertNotIn("tiktok", call["json"])
        self.assertNotIn("secret-pay@example.com", json.dumps(call["json"]))
        self.assertEqual(session.timeout.total, TIMEOUT_SECONDS)

    async def test_already_accepted_and_data_wrapper(self):
        _session, result, _sleep = await self._request(
            [FakeResponse(200, {"data": {"inviteUrl": LOGIN_URL, "creatorId": "cr_9", "alreadyAccepted": True}})]
        )
        self.assertTrue(result.already_accepted)
        self.assertEqual(result.creator_id, "cr_9")

    async def test_unauthorized_is_not_retried_and_hides_the_secret(self):
        session, error, sleep = await self._request(
            [FakeResponse(401, {"code": "UNAUTHORIZED", "message": f"leaked {SECRET}"})]
        )
        self.assertIsInstance(error, CreatorInviteError)
        self.assertEqual(len(session.calls), 1)
        sleep.assert_not_awaited()
        self.assertNotIn(SECRET, error.public_message())

    async def test_conflict_is_not_retried(self):
        session, error, sleep = await self._request(
            [FakeResponse(409, {"code": "DISCORD_IDENTITY_CONFLICT"})]
        )
        self.assertEqual(len(session.calls), 1)
        sleep.assert_not_awaited()
        self.assertIn("DISCORD_IDENTITY_CONFLICT", error.public_message())

    async def test_retries_then_succeeds(self):
        session, result, sleep = await self._request(
            [
                TimeoutError(),
                FakeResponse(503, {}),
                FakeResponse(200, {"inviteUrl": INVITE_URL, "creatorId": "cr_1", "alreadyAccepted": False}),
            ]
        )
        self.assertIsInstance(result, InviteResult)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_missing_configuration_skips_http(self):
        os.environ.pop("CREATOR_BOT_API_BASE_URL", None)
        os.environ["CREATOR_BOT_INVITE_SECRET"] = SECRET
        session, factory = self._session([])
        with patch("creator_invite.aiohttp.ClientSession", factory):
            with self.assertRaises(CreatorInviteError) as caught:
                await request_creator_invite(_record())
        self.assertEqual(session.calls, [])
        self.assertNotIn(SECRET, caught.exception.public_message())
        self.assertIn("CREATOR_BOT_API_BASE_URL", caught.exception.public_message())

    async def test_secret_shaped_error_code_is_redacted(self):
        session, error, _sleep = await self._request([FakeResponse(401, {"code": SECRET})])
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn(SECRET, error.public_message())
        self.assertIn("[redacted]", error.public_message())


class CopyTests(CreatorInviteTestCase):
    def test_paragraph_and_backfill_dm_have_no_long_dashes(self):
        fresh = invite_paragraph(INVITE_URL)
        accepted = invite_paragraph(LOGIN_URL, already_accepted=True)
        dm = backfill_dm(INVITE_URL)
        failure = invite_failure_message(_record(), "UNAUTHORIZED (HTTP 401)")
        for text in (fresh, accepted, dm, failure):
            _assert_no_long_dashes(self, text)
        self.assertIn("valable 30 jours", fresh)
        self.assertIn(INVITE_URL, fresh)
        self.assertIn("lien de connexion à ton espace", accepted)
        self.assertNotIn("30 jours", accepted)
        self.assertIn(fresh, dm)
        self.assertNotIn(SECRET, failure)
        self.assertIn("partis quand même", failure)

    def test_approval_dm_keeps_the_current_text_and_appends_the_link(self):
        plain = registration._approval_dm()
        linked = registration._approval_dm(INVITE_URL)
        accepted = registration._approval_dm(LOGIN_URL, already_accepted=True)
        self.assertTrue(plain.endswith("Tes infos de paiement sont bien enregistrées."))
        self.assertIn("Ta demande a été acceptée", plain)
        self.assertIn("Générer du contenu", plain)
        self.assertNotIn("espace Creator", plain)
        self.assertTrue(linked.startswith(plain + "\n\n"))
        self.assertIn(INVITE_URL, linked)
        self.assertIn("valable 30 jours", linked)
        self.assertIn("lien de connexion à ton espace", accepted)
        self.assertNotIn("30 jours", accepted)
        for text in (plain, linked, accepted):
            _assert_no_long_dashes(self, text)

    def test_failure_message_redacts_a_secret(self):
        os.environ["CREATOR_BOT_INVITE_SECRET"] = SECRET
        text = invite_failure_message(_record(), f"boom {SECRET}")
        self.assertNotIn(SECRET, text)
        self.assertIn("[redacted]", text)


class ReviewTests(CreatorInviteTestCase, unittest.IsolatedAsyncioTestCase):
    def _interaction(self, user, channel, guild=None):
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=7, name="staff"),
            client=FakeClient(user, channel),
            guild=guild,
            message=None,
            channel_id=555,
            response=FakeResponseInteraction(),
            followup=FakeFollowup(),
        )
        return interaction

    async def _approve(self, session_responses, *, user_fail=None, invite_side_effect=None, configured=True):
        if configured:
            os.environ["CREATOR_BOT_API_BASE_URL"] = "https://creator.example"
            os.environ["CREATOR_BOT_INVITE_SECRET"] = SECRET
        else:
            os.environ.pop("CREATOR_BOT_API_BASE_URL", None)
            os.environ["CREATOR_BOT_INVITE_SECRET"] = SECRET
        registration._registrations = {"42": _record()}
        registration._store_loaded = True
        user = FakeUser(42, "alice", global_name="Alice Global", fail=user_fail)
        channel = FakeChannel()
        guild = FakeGuild("Alice N.")
        interaction = self._interaction(user, channel, guild)
        session, factory = self._session(session_responses)
        stdout = io.StringIO()
        with (
            patch("registration._is_staff", return_value=True),
            patch("registration.grant_creator_role", new=AsyncMock(return_value=True)) as grant,
            patch("creator_invite.aiohttp.ClientSession", factory),
            patch("creator_invite.asyncio.sleep", new=AsyncMock()),
            contextlib.redirect_stdout(stdout),
        ):
            if invite_side_effect is not None:
                with patch("registration.request_creator_invite", new=AsyncMock(side_effect=invite_side_effect)):
                    await registration.review_registration(interaction, 42, "approved")
            else:
                await registration.review_registration(interaction, 42, "approved")
        return interaction, user, channel, grant, session, stdout.getvalue()

    async def test_api_failure_still_approves_and_sends_the_current_dm(self):
        interaction, user, channel, grant, session, logs = await self._approve(
            [FakeResponse(401, {"code": "UNAUTHORIZED", "message": SECRET})]
        )
        stored = registration._registrations["42"]
        self.assertEqual(stored["status"], "approved")
        self.assertFalse(stored["creator_invite_sent"])
        grant.assert_awaited()
        self.assertTrue(interaction.response.deferred)
        self.assertEqual(user.sent, [registration._approval_dm()])
        self.assertNotIn("espace Creator", user.sent[0])
        self.assertIn("Validée", interaction.followup.messages[0])
        self.assertIn("Lien Creator non envoyé", interaction.followup.messages[0])
        self.assertIn("UNAUTHORIZED", interaction.followup.messages[0])
        self.assertEqual(len(channel.sent), 1)
        self.assertIn("partis quand même", channel.sent[0]["content"])
        self.assertNotIn(SECRET, user.sent[0])
        self.assertNotIn(SECRET, interaction.followup.messages[0])
        self.assertNotIn(SECRET, channel.sent[0]["content"])
        self.assertNotIn(SECRET, logs)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]["json"]["displayName"], "Alice N.")
        self.assertEqual(session.calls[0]["headers"]["x-creator-bot-invite-secret"], SECRET)

    async def test_missing_configuration_does_not_block_approval(self):
        interaction, user, channel, grant, session, logs = await self._approve([], configured=False)
        self.assertEqual(registration._registrations["42"]["status"], "approved")
        grant.assert_awaited()
        self.assertEqual(user.sent, [registration._approval_dm()])
        self.assertEqual(session.calls, [])
        self.assertIn("configuration manquante", channel.sent[0]["content"])
        self.assertNotIn(SECRET, channel.sent[0]["content"])
        self.assertNotIn(SECRET, logs)
        self.assertNotIn(SECRET, user.sent[0])

    async def test_success_appends_the_personal_link(self):
        interaction, user, channel, _grant, session, _logs = await self._approve(
            [FakeResponse(200, {"inviteUrl": INVITE_URL, "creatorId": "cr_1", "alreadyAccepted": False})]
        )
        stored = registration._registrations["42"]
        self.assertTrue(stored["creator_invite_sent"])
        self.assertEqual(stored["creator_platform_id"], "cr_1")
        self.assertEqual(user.sent, [registration._approval_dm(INVITE_URL)])
        self.assertIn("Lien Creator inclus", interaction.followup.messages[0])
        self.assertEqual(channel.sent, [])
        self.assertEqual(len(session.calls), 1)
        notice = registration._notice_text(stored)
        self.assertIn(registration.INVITE_SENT_MARKER, notice)

    async def test_already_accepted_uses_the_login_wording(self):
        _interaction, user, _channel, _grant, _session, _logs = await self._approve(
            [FakeResponse(200, {"inviteUrl": LOGIN_URL, "creatorId": "cr_1", "alreadyAccepted": True})]
        )
        self.assertIn("lien de connexion à ton espace", user.sent[0])
        self.assertNotIn("30 jours", user.sent[0])
        self.assertIn("Tes infos de paiement sont bien enregistrées.", user.sent[0])

    async def test_unexpected_error_still_sends_the_dm_without_the_secret(self):
        interaction, user, channel, grant, _session, logs = await self._approve(
            [],
            invite_side_effect=RuntimeError(f"header {SECRET}"),
        )
        self.assertEqual(user.sent, [registration._approval_dm()])
        grant.assert_awaited()
        self.assertIn("erreur inattendue", interaction.followup.messages[0])
        self.assertNotIn(SECRET, interaction.followup.messages[0])
        self.assertNotIn(SECRET, channel.sent[0]["content"])
        self.assertNotIn(SECRET, logs)
        self.assertIn("RuntimeError", logs)

    async def test_closed_dm_does_not_mark_the_link_as_sent(self):
        interaction, user, _channel, _grant, _session, _logs = await self._approve(
            [FakeResponse(200, {"inviteUrl": INVITE_URL, "creatorId": "cr_1", "alreadyAccepted": False})],
            user_fail=discord.Forbidden(_HttpResponse(), "closed"),
        )
        self.assertEqual(user.sent, [])
        self.assertIsNot(registration._registrations["42"].get("creator_invite_sent"), True)
        self.assertIn("n’a pas été délivré", interaction.followup.messages[0])

    async def test_reject_does_not_call_the_api(self):
        registration._registrations = {"42": _record()}
        registration._store_loaded = True
        user = FakeUser(42, "alice")
        channel = FakeChannel()
        interaction = self._interaction(user, channel)
        session, factory = self._session([FakeResponse(500, {})])
        with (
            patch("registration._is_staff", return_value=True),
            patch("creator_invite.aiohttp.ClientSession", factory),
        ):
            await registration.review_registration(interaction, 42, "rejected")
        self.assertEqual(session.calls, [])
        self.assertEqual(registration._registrations["42"]["status"], "rejected")
        self.assertEqual(channel.sent, [])
        self.assertIn("n’a pas été retenue", user.sent[0])


class LogRoundtripTests(CreatorInviteTestCase):
    def test_sent_notice_roundtrips_and_missing_notice_does_not_count_as_sent(self):
        sent = registration.registration_embed(
            _record(status="approved", dm_sent=True, creator_invite_sent=True, validated_by=None)
        )
        missing = registration.registration_embed(
            _record(
                status="approved",
                dm_sent=True,
                creator_invite_sent=False,
                creator_invite_error="UNAUTHORIZED (HTTP 401)",
            )
        )

        class Message:
            def __init__(self, embed):
                self.embeds = [embed]
                self.created_at = datetime(2026, 11, 9, tzinfo=timezone.utc)
                self.edited_at = None
                self.id = 7
                self.channel = SimpleNamespace(id=555)
                self.content = ""

        parsed_sent = registration._record_from_message(Message(sent))
        parsed_missing = registration._record_from_message(Message(missing))
        self.assertTrue(parsed_sent["creator_invite_sent"])
        self.assertFalse(parsed_missing["creator_invite_sent"])
        self.assertNotIn(registration.INVITE_SENT_MARKER, registration._notice_text(parsed_missing))
        self.assertIsNone(registration.invite_sent_discord_id("Lien Creator non envoyé pour <@42>."))
        self.assertEqual(
            registration.invite_sent_discord_id("Lien Creator envoyé à <@42> (@alice)."),
            "42",
        )


class BackfillTests(CreatorInviteTestCase, unittest.IsolatedAsyncioTestCase):
    def test_plan_keeps_only_approved_and_skips_sent(self):
        records = [
            _record(discord_id="1", discord_username="ann", status="approved", applied_at="2026-11-02T00:00:00+00:00"),
            _record(discord_id="2", discord_username="bob", status="pending"),
            _record(discord_id="3", discord_username="cam", status="approved", creator_invite_sent=True),
            _record(discord_id="4", discord_username="dee", status="rejected"),
            _record(discord_id="5", discord_username="eve", status="approved", applied_at="2026-11-01T00:00:00+00:00"),
        ]
        state = {"sent": {"5": {"sent_at": "2026-11-08T00:00:00+00:00"}}}
        targets, skipped = plan_backfill(records, state, limit=10)
        self.assertEqual([record["discord_id"] for record in targets], ["1"])
        self.assertEqual({record["discord_id"] for record in skipped}, {"3", "5"})
        only, _skipped = plan_backfill(records, {"sent": {}}, only="2")
        self.assertEqual(only, [])
        limited, _skipped = plan_backfill(records, {"sent": {}}, limit=1)
        self.assertEqual([record["discord_id"] for record in limited], ["5"])

    def test_state_roundtrip(self):
        path = Path(self.tmp.name) / "state.json"
        save_state(path, {"sent": {"42": {"sent_at": "2026-11-09T00:00:00+00:00", "username": "alice"}}})
        self.assertIn("42", load_state(path)["sent"])

    def test_dry_run_text_shows_id_username_and_dm_without_calling_out(self):
        targets = [_record(status="approved")]
        text = format_dry_run(source="store /tmp/registrations.json (1 approuvée)", targets=targets, skipped=[])
        self.assertIn("42", text)
        self.assertIn("@alice", text)
        self.assertIn(backfill_dm(DRY_RUN_INVITE_URL), text)
        self.assertIn("Aucun appel à l’API Creator, aucun DM", text)
        self.assertIn("<inviteUrl>", text)
        _assert_no_long_dashes(self, text)
        self.assertNotIn("secret-pay@example.com", text)

    async def test_dry_run_command_prints_the_dm_and_does_not_call_the_api(self):
        registration._registrations = {}
        registration._store_loaded = False
        self.store_path.write_text(
            json.dumps({"registrations": [_record(status="approved")]}),
            encoding="utf-8",
        )
        session, factory = self._session([FakeResponse(500, {"code": "INVITE_FAILED"})])
        stdout = io.StringIO()
        args = backfill.build_parser().parse_args(["--only", "42"])
        with (
            patch("creator_invite.aiohttp.ClientSession", factory),
            contextlib.redirect_stdout(stdout),
        ):
            code = await backfill.async_main(args)
        self.assertEqual(code, 0)
        self.assertEqual(session.calls, [])
        printed = stdout.getvalue()
        self.assertIn("42", printed)
        self.assertIn("@alice", printed)
        self.assertIn("Suis tes vues et tes paiements", printed)
        self.assertIn("valable 30 jours", printed)
        self.assertIn("dry-run", printed)
        self.assertNotIn(SECRET, printed)

    async def test_send_marks_success_skips_closed_dms_and_pauses(self):
        records = [
            _record(discord_id="1", discord_username="ann", status="approved"),
            _record(discord_id="2", discord_username="bob", status="approved"),
        ]
        registration._registrations = {record["discord_id"]: record for record in records}
        registration._store_loaded = True
        state = {"sent": {}}
        state_path = Path(self.tmp.name) / "state.json"

        async def deliver(_client, record):
            if record["discord_id"] == "1":
                raise discord.Forbidden(_HttpResponse(), "closed")
            return InviteResult(INVITE_URL, "cr_2", False)

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with (
            patch.object(backfill, "_note_on_log", new=AsyncMock()),
            patch.object(backfill.asyncio, "sleep", fake_sleep),
        ):
            code = await backfill._send_all(
                SimpleNamespace(),
                records,
                state,
                state_path=state_path,
                deliver=deliver,
            )
        self.assertEqual(code, 1)
        self.assertNotIn("1", state["sent"])
        self.assertIn("2", state["sent"])
        self.assertNotIn("invite_url", json.dumps(state))
        self.assertTrue(registration._registrations["2"]["creator_invite_sent"])
        self.assertIsNot(registration._registrations["1"].get("creator_invite_sent"), True)
        self.assertEqual(sleeps, [backfill.DM_PAUSE_SECONDS])
        again, skipped = plan_backfill(list(registration._registrations.values()), load_state(state_path))
        self.assertEqual([record["discord_id"] for record in again], ["1"])
        self.assertEqual([record["discord_id"] for record in skipped], ["2"])

    async def test_send_without_token_does_not_call_the_api(self):
        self.store_path.write_text(json.dumps({"registrations": []}), encoding="utf-8")
        session, factory = self._session([])
        args = backfill.build_parser().parse_args(["--send"])
        with patch("creator_invite.aiohttp.ClientSession", factory):
            code = await backfill.async_main(args)
        self.assertEqual(code, 2)
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
