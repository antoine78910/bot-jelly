"""Payload, embed, auth, and webhook tests for creator video notifications."""

from __future__ import annotations

import os
import re
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import discord
from aiohttp.test_utils import TestClient, TestServer

from creator_activity import (
    ACCENT_COLOR,
    EMBED_TOTAL_CHAR_LIMIT,
    VIDEO_ID_CACHE_SIZE,
    build_inactive_embeds,
    build_sample_creator_video,
    build_video_embed,
    build_video_view,
    clear_seen_videos,
    creator_video_channel_id,
    embed_char_count,
    forget_video,
    parse_creator_video,
    parse_inactive_report,
    post_inactive_creators,
    remember_video,
    video_already_posted,
    PayloadError,
)
from creator_signups import CREATOR_VIDEO_WEBHOOK_ENVS, verify_webhook_secret
from creator_signup_webhook import create_webhook_app

LONG_DASHES = ("—", "–")
VIDEO_SECRET = "video-secret"
SIGNUP_SECRET = "signup-secret"


def _video_payload(**overrides):
    payload = {
        "video_id": "vid-1",
        "creator_id": "creator-1",
        "creator_name": "Marie Dupont",
        "handle": "@marie.jobsearch",
        "platform": "tiktok",
        "url": "https://www.tiktok.com/@marie.jobsearch/video/123",
        "published_at": "2026-11-08T08:11:00Z",
    }
    payload.update(overrides)
    return payload


def _inactive_creator(**overrides):
    creator = {
        "creator_id": "creator-1",
        "creator_name": "Marie Dupont",
        "handles": [
            {"platform": "tiktok", "handle": "marie"},
            {"platform": "instagram", "handle": "marie.ig"},
        ],
        "last_published_at": "2026-11-05T12:00:00Z",
        "affiliate_only": False,
    }
    creator.update(overrides)
    return creator


def _inactive_payload(creators):
    return {"date": "2026-11-08", "creators": creators}


class _HttpResponse:
    status = 403
    reason = "Forbidden"


class FakeMessage:
    jump_url = "https://discord.com/channels/1/2/3"
    id = 1


class FakeChannel:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, **kwargs):
        if self.fail:
            raise discord.HTTPException(_HttpResponse(), "missing access")
        self.sent.append(kwargs)
        return FakeMessage()


def _blob(embeds) -> str:
    parts = []
    for embed in embeds:
        parts.append(embed.title or "")
        parts.append(embed.description or "")
        if embed.footer and embed.footer.text:
            parts.append(embed.footer.text)
        for field in embed.fields:
            parts.append(field.name)
            parts.append(field.value)
    return "\n".join(parts)


def _assert_no_long_dashes(testcase, text):
    for dash in LONG_DASHES:
        testcase.assertNotIn(dash, text)


class CreatorActivityTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "CREATOR_VIDEO_WEBHOOK_SECRET",
                "CREATOR_SIGNUP_WEBHOOK_SECRET",
                "CREATOR_VIDEO_CHANNEL_ID",
            )
        }
        os.environ["CREATOR_VIDEO_WEBHOOK_SECRET"] = VIDEO_SECRET
        os.environ["CREATOR_SIGNUP_WEBHOOK_SECRET"] = SIGNUP_SECRET
        os.environ.pop("CREATOR_VIDEO_CHANNEL_ID", None)
        clear_seen_videos()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        clear_seen_videos()


class PayloadValidationTests(CreatorActivityTestCase):
    def test_video_payload_builds_french_embed_with_links_and_buttons(self):
        video = parse_creator_video(_video_payload())
        embed = build_video_embed(video)
        view = build_video_view(video)
        fields = {field.name: field.value for field in embed.fields}
        published = datetime(2026, 11, 8, 8, 11, tzinfo=timezone.utc)

        self.assertEqual(embed.title, "Nouvelle vidéo de Marie Dupont")
        self.assertEqual(embed.color.value, ACCENT_COLOR)
        self.assertEqual(fields["Plateforme"], "TikTok")
        self.assertEqual(
            fields["Compte"],
            "[@marie.jobsearch](https://www.tiktok.com/@marie.jobsearch)",
        )
        self.assertEqual(fields["Publiée"], f"<t:{int(published.timestamp())}:F>")
        self.assertEqual(
            [button.label for button in view.children],
            ["Voir la vidéo", "Voir le compte"],
        )
        self.assertEqual(view.children[0].url, video.url)
        self.assertEqual(view.children[1].url, video.profile_url)
        _assert_no_long_dashes(self, _blob([embed]))
        for button in view.children:
            _assert_no_long_dashes(self, button.label)

    def test_profile_url_and_admin_link(self):
        video = parse_creator_video(
            _video_payload(
                platform="Instagram",
                handle="marie.ig",
                profile_url="https://example.com/marie",
                creator_admin_url="https://example.com/admin/creators/creator-1",
            )
        )
        embed = build_video_embed(video)
        view = build_video_view(video)
        fields = {field.name: field.value for field in embed.fields}

        self.assertEqual(fields["Plateforme"], "Instagram")
        self.assertEqual(fields["Compte"], "[@marie.ig](https://example.com/marie)")
        self.assertEqual(
            [button.label for button in view.children],
            ["Voir la vidéo", "Voir le compte", "Fiche créateur"],
        )
        self.assertEqual(view.children[1].url, "https://example.com/marie")
        self.assertEqual(view.children[2].url, "https://example.com/admin/creators/creator-1")

    def test_instagram_default_profile_url(self):
        video = parse_creator_video(
            _video_payload(platform="instagram", handle="marie.ig", video_id="vid-ig")
        )
        self.assertEqual(video.profile_url, "https://www.instagram.com/marie.ig/")

    def test_long_dash_in_name_is_replaced(self):
        video = parse_creator_video(_video_payload(creator_name="Marie — Dupont"))
        self.assertEqual(video.creator_name, "Marie - Dupont")
        self.assertNotIn("—", build_video_embed(video).title)

    def test_record_wrapper_is_accepted(self):
        video = parse_creator_video({"type": "INSERT", "record": _video_payload(video_id="wrapped")})
        self.assertEqual(video.video_id, "wrapped")

    def test_video_payload_rejects_bad_fields(self):
        cases = [
            _video_payload(platform="youtube"),
            _video_payload(url="not a url"),
            _video_payload(url="javascript:alert(1)"),
            _video_payload(published_at="yesterday"),
            _video_payload(handle="bad handle"),
            _video_payload(creator_name="  "),
            {key: value for key, value in _video_payload().items() if key != "video_id"},
            _video_payload(profile_url="notaurl"),
            _video_payload(creator_admin_url="notaurl"),
        ]
        for payload in cases:
            with self.assertRaises(PayloadError):
                parse_creator_video(payload)

    def test_inactive_links_days_and_never_posted(self):
        report = parse_inactive_report(
            _inactive_payload(
                [
                    _inactive_creator(),
                    _inactive_creator(
                        creator_id="creator-2",
                        creator_name="Luc Martin",
                        handles=[{"platform": "tiktok", "handle": "luc", "profile_url": "https://example.com/luc"}],
                        last_published_at="2026-11-07T20:00:00Z",
                        affiliate_only=True,
                    ),
                    _inactive_creator(
                        creator_id="creator-3",
                        creator_name="No Posts",
                        handles=[{"platform": "instagram", "handle": "noposts"}],
                        last_published_at=None,
                    ),
                    _inactive_creator(
                        creator_id="creator-4",
                        creator_name="Aujourd'hui",
                        handles=[{"platform": "tiktok", "handle": "today"}],
                        last_published_at="2026-11-08T22:00:00Z",
                    ),
                ]
            )
        )
        self.assertEqual(report.as_of, datetime(2026, 11, 8, 23, 59, 59, tzinfo=timezone.utc))
        embeds = build_inactive_embeds(report)
        text = _blob(embeds)

        self.assertEqual(embeds[0].title, "Créateurs sans post depuis plus de 24 h")
        self.assertEqual(embeds[0].color.value, ACCENT_COLOR)
        self.assertIn("[@marie](https://www.tiktok.com/@marie)", text)
        self.assertIn("[@marie.ig](https://www.instagram.com/marie.ig/)", text)
        self.assertIn("[@luc](https://example.com/luc)", text)
        self.assertIn("dernier post il y a 3 jours", text)
        self.assertIn("dernier post il y a 1 jour", text)
        self.assertNotIn("dernier post il y a 1 jours", text)
        self.assertIn("jamais posté", text)
        self.assertIn("dernier post il y a moins d'un jour", text)
        self.assertIn("affiliation uniquement", text)
        self.assertNotIn("<@", text)
        _assert_no_long_dashes(self, text)

    def test_empty_inactive_list_is_a_short_message(self):
        report = parse_inactive_report(_inactive_payload([]))
        embeds = build_inactive_embeds(report)
        text = _blob(embeds)
        self.assertEqual(len(embeds), 1)
        self.assertIn("Tous les créateurs actifs ont posté.", text)
        self.assertNotIn("jamais posté", text)

    def test_inactive_payload_rejects_bad_fields(self):
        cases = [
            {"creators": []},
            {"date": "nope", "creators": []},
            {"date": "2026-11-08", "creators": "nope"},
            _inactive_payload([{"creator_id": "c", "creator_name": "A"}]),
            _inactive_payload(
                [
                    _inactive_creator(
                        handles=[{"platform": "youtube", "handle": "marie"}],
                    )
                ]
            ),
            _inactive_payload(
                [_inactive_creator(last_published_at="not-a-date")]
            ),
        ]
        for payload in cases:
            with self.assertRaises(PayloadError):
                parse_inactive_report(payload)

    def test_large_digest_stays_one_message_within_discord_limits(self):
        creators = []
        for index in range(80):
            creators.append(
                _inactive_creator(
                    creator_id=f"creator-{index}",
                    creator_name=f"NomUnique{index:03d}",
                    handles=[
                        {"platform": "tiktok", "handle": f"tt{index:03d}"},
                        {"platform": "instagram", "handle": f"ig{index:03d}"},
                    ],
                    last_published_at="2026-11-01T08:00:00Z",
                )
            )
        embeds = build_inactive_embeds(parse_inactive_report(_inactive_payload(creators)))
        text = _blob(embeds)
        total = sum(embed_char_count(embed) for embed in embeds)

        self.assertLessEqual(len(embeds), 10)
        self.assertLessEqual(total, EMBED_TOTAL_CHAR_LIMIT)
        self.assertGreater(len(embeds), 1)
        self.assertIn("NomUnique000", text)
        self.assertNotIn("NomUnique079", text)
        self.assertIn("non affiché", text)
        self.assertNotRegex(text, r"TikTok @[\w.-]+(?!\])")
        self.assertNotRegex(text, r"Instagram @[\w.-]+(?!\])")
        self.assertRegex(text, r"\[@tt\d{3}\]\(https://www\.tiktok\.com/@tt\d{3}\)")
        _assert_no_long_dashes(self, text)

    def test_full_timestamp_is_not_forced_to_end_of_day(self):
        report = parse_inactive_report(
            {
                "date": "2026-11-08T12:00:00Z",
                "creators": [
                    _inactive_creator(last_published_at="2026-11-07T12:00:00Z", handles=[])
                ],
            }
        )
        text = _blob(build_inactive_embeds(report))
        self.assertEqual(report.as_of, datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc))
        self.assertIn("dernier post il y a 1 jour", text)

    def test_sample_video_has_profile_button(self):
        video = build_sample_creator_video()
        view = build_video_view(video)
        self.assertEqual(view.children[1].label, "Voir le compte")
        self.assertEqual(view.children[1].url, "https://www.tiktok.com/@marie.jobsearch")
        self.assertIn("Nouvelle vidéo de Marie Dupont", build_video_embed(video).title)

    def test_lru_evicts_oldest_and_forget_allows_retry(self):
        for index in range(VIDEO_ID_CACHE_SIZE):
            self.assertTrue(remember_video(str(index)))
        self.assertFalse(remember_video("0"))
        self.assertTrue(remember_video("fresh"))
        self.assertFalse(video_already_posted("1"))
        self.assertTrue(video_already_posted("0"))
        self.assertTrue(video_already_posted("fresh"))
        forget_video("fresh")
        self.assertTrue(remember_video("fresh"))

    def test_channel_id_config_and_env_override(self):
        self.assertEqual(creator_video_channel_id(), 1568899413471002786)
        os.environ["CREATOR_VIDEO_CHANNEL_ID"] = "42"
        self.assertEqual(creator_video_channel_id(), 42)

    def test_video_secret_falls_back_and_signup_secret_stays_separate(self):
        headers = {"X-Webhook-Secret": SIGNUP_SECRET}
        self.assertTrue(verify_webhook_secret(headers))
        self.assertFalse(
            verify_webhook_secret({"X-Webhook-Secret": VIDEO_SECRET})
        )
        self.assertFalse(
            verify_webhook_secret(headers, env_names=CREATOR_VIDEO_WEBHOOK_ENVS)
        )
        self.assertTrue(
            verify_webhook_secret(
                {"Authorization": f"Bearer {VIDEO_SECRET}"},
                env_names=CREATOR_VIDEO_WEBHOOK_ENVS,
            )
        )

        os.environ.pop("CREATOR_VIDEO_WEBHOOK_SECRET", None)
        self.assertTrue(
            verify_webhook_secret(headers, env_names=CREATOR_VIDEO_WEBHOOK_ENVS)
        )

        os.environ.pop("CREATOR_SIGNUP_WEBHOOK_SECRET", None)
        self.assertTrue(
            verify_webhook_secret({}, env_names=CREATOR_VIDEO_WEBHOOK_ENVS)
        )


class WebhookHttpTests(CreatorActivityTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.channel = FakeChannel()
        self.app = create_webhook_app(object())
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    def _headers(self, secret=VIDEO_SECRET):
        return {"X-Webhook-Secret": secret}

    async def test_video_posts_once_and_dedupes(self):
        with patch(
            "creator_activity.resolve_creator_video_channel",
            new=AsyncMock(return_value=self.channel),
        ):
            first = await self.client.post(
                "/webhooks/creator-video",
                json=_video_payload(),
                headers=self._headers(),
            )
            second = await self.client.post(
                "/webhooks/creator-video",
                json=_video_payload(url="https://example.com/other"),
                headers=self._headers(),
            )
            other = await self.client.post(
                "/webhooks/creator-video",
                json=_video_payload(video_id="vid-2"),
                headers=self._headers(),
            )

        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(other.status, 200)
        self.assertEqual(len(self.channel.sent), 2)
        sent = self.channel.sent[0]
        self.assertIsNone(sent.get("content"))
        self.assertFalse(sent["allowed_mentions"].everyone)
        self.assertFalse(sent["allowed_mentions"].users)
        self.assertFalse(sent["allowed_mentions"].roles)
        self.assertEqual(sent["embed"].title, "Nouvelle vidéo de Marie Dupont")
        self.assertEqual(sent["view"].children[1].label, "Voir le compte")

    async def test_video_auth_and_payload_errors(self):
        denied = await self.client.post(
            "/webhooks/creator-video",
            json=_video_payload(),
            headers=self._headers(SIGNUP_SECRET),
        )
        self.assertEqual(denied.status, 401)

        missing = await self.client.post(
            "/webhooks/creator-video",
            data="{",
            headers={**self._headers(), "Content-Type": "application/json"},
        )
        self.assertEqual(missing.status, 400)

        bad = await self.client.post(
            "/webhooks/creator-video",
            json=_video_payload(platform="youtube"),
            headers=self._headers(),
        )
        self.assertEqual(bad.status, 400)
        self.assertIn("platform", (await bad.text()).lower())

        not_object = await self.client.post(
            "/webhooks/creator-video",
            json=[_video_payload()],
            headers=self._headers(),
        )
        self.assertEqual(not_object.status, 400)

    async def test_channel_unavailable_is_retryable(self):
        flaky = FakeChannel()

        async def send(**kwargs):
            if not flaky.sent and not getattr(flaky, "failed", False):
                flaky.failed = True
                raise discord.HTTPException(_HttpResponse(), "missing access")
            flaky.sent.append(kwargs)
            return FakeMessage()

        flaky.send = send
        resolutions = [None, flaky, flaky]

        async def resolve(_client):
            return resolutions.pop(0)

        with patch("creator_activity.resolve_creator_video_channel", new=resolve):
            unavailable = await self.client.post(
                "/webhooks/creator-video",
                json=_video_payload(video_id="retry-me"),
                headers=self._headers(),
            )
            rejected = await self.client.post(
                "/webhooks/creator-video",
                json=_video_payload(video_id="retry-me"),
                headers=self._headers(),
            )
            posted = await self.client.post(
                "/webhooks/creator-video",
                json=_video_payload(video_id="retry-me"),
                headers=self._headers(),
            )

        self.assertEqual(unavailable.status, 503)
        self.assertEqual(rejected.status, 503)
        self.assertEqual(posted.status, 200)
        self.assertEqual(len(flaky.sent), 1)

    async def test_inactive_empty_and_populated_post_once_without_pings(self):
        with patch(
            "creator_activity.resolve_creator_video_channel",
            new=AsyncMock(return_value=self.channel),
        ):
            empty = await self.client.post(
                "/webhooks/creator-inactive",
                json=_inactive_payload([]),
                headers=self._headers(),
            )
            filled = await self.client.post(
                "/webhooks/creator-inactive",
                json=_inactive_payload([_inactive_creator()]),
                headers=self._headers(),
            )
            denied = await self.client.post(
                "/webhooks/creator-inactive",
                json=_inactive_payload([]),
                headers=self._headers("nope"),
            )

        self.assertEqual(empty.status, 200)
        self.assertEqual(filled.status, 200)
        self.assertEqual(denied.status, 401)
        self.assertEqual(len(self.channel.sent), 2)
        for sent in self.channel.sent:
            self.assertIsNone(sent.get("content"))
            self.assertFalse(sent["allowed_mentions"].everyone)
            self.assertFalse(sent["allowed_mentions"].users)
            self.assertEqual(len(sent["embeds"]), 1)
        self.assertIn("Tous les créateurs actifs ont posté.", _blob(self.channel.sent[0]["embeds"]))
        filled_text = _blob(self.channel.sent[1]["embeds"])
        self.assertIn("[@marie](https://www.tiktok.com/@marie)", filled_text)
        self.assertIn("dernier post il y a 3 jours", filled_text)

    async def test_inactive_channel_unavailable_and_bad_payload(self):
        with patch(
            "creator_activity.resolve_creator_video_channel",
            new=AsyncMock(return_value=None),
        ):
            unavailable = await self.client.post(
                "/webhooks/creator-inactive",
                json=_inactive_payload([]),
                headers=self._headers(),
            )
        bad = await self.client.post(
            "/webhooks/creator-inactive",
            json={"date": "2026-11-08"},
            headers=self._headers(),
        )
        self.assertEqual(unavailable.status, 503)
        self.assertEqual(bad.status, 400)

    async def test_large_inactive_digest_is_a_single_send(self):
        creators = [
            _inactive_creator(
                creator_id=f"creator-{index}",
                creator_name=f"NomUnique{index:03d}",
                handles=[{"platform": "tiktok", "handle": f"tt{index:03d}"}],
            )
            for index in range(80)
        ]
        with patch(
            "creator_activity.resolve_creator_video_channel",
            new=AsyncMock(return_value=self.channel),
        ):
            response = await self.client.post(
                "/webhooks/creator-inactive",
                json=_inactive_payload(creators),
                headers=self._headers(),
            )
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.channel.sent), 1)
        embeds = self.channel.sent[0]["embeds"]
        self.assertLessEqual(len(embeds), 10)
        self.assertLessEqual(sum(embed_char_count(embed) for embed in embeds), EMBED_TOTAL_CHAR_LIMIT)

    async def test_signup_route_keeps_its_own_secret(self):
        wrong = await self.client.post(
            "/webhooks/creator-signup",
            json={},
            headers=self._headers(VIDEO_SECRET),
        )
        ignored = await self.client.post(
            "/webhooks/creator-signup",
            json={},
            headers=self._headers(SIGNUP_SECRET),
        )
        self.assertEqual(wrong.status, 401)
        self.assertEqual(ignored.status, 202)

    async def test_post_inactive_mentions_nobody(self):
        report = parse_inactive_report(_inactive_payload([_inactive_creator()]))
        with patch(
            "creator_activity.resolve_creator_video_channel",
            new=AsyncMock(return_value=self.channel),
        ):
            await post_inactive_creators(object(), report)
        sent = self.channel.sent[0]
        self.assertNotIn("content", sent)
        self.assertFalse(sent["allowed_mentions"].roles)
        self.assertFalse(sent["allowed_mentions"].replied_user)


if __name__ == "__main__":
    unittest.main()
