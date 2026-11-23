"""Vidéo button and staff commands, end to end with offline Discord doubles."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import content_generator
import persona
import video_generator
import video_hosting
import video_store
from discord_fakes import (
    FakeGuild,
    FakeInteraction,
    make_attachment,
    make_channel,
    make_member,
    view_urls,
)
from registration import creator_role_id
from video_store import VideoStore
from video_uniquify import media, pipeline
from video_uniquify.frames import CANVAS_H, CANVAS_W, sample_params

LONG_DASHES = ("—", "–", "―")
REPO = Path(__file__).resolve().parents[1]


def _probe(duration=20.0, audio=True):
    streams = [{"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920,
                "r_frame_rate": "60/1"}]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000"})
    return {"streams": streams, "format": {"duration": str(duration)}}


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = VideoStore(self.tmp / "data")
        video_store.reset_store_for_tests(self.store)
        video_generator._queue = None
        video_generator._started = False
        video_generator._selfcheck_problems = None
        self.patches = [
            mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://bot.example", "VIDEO_QUEUE_MAX": "3"}),
            mock.patch.object(video_hosting, "_routes_registered", True),
            mock.patch.object(media, "selfcheck", return_value=[]),
            mock.patch.object(persona, "is_configured", return_value=False),
            mock.patch.object(persona, "_is_approved_creator", return_value=False),
        ]
        for p in self.patches:
            p.start()
        self.render_calls: list[dict] = []
        self.render_gate: asyncio.Event | None = None
        self.guild = FakeGuild()
        self.channel = make_channel(self.guild)
        self.stdout = io.StringIO()
        self._redirect = contextlib.redirect_stdout(self.stdout)
        self._redirect.__enter__()
        await video_generator.start()

    async def asyncTearDown(self):
        await video_generator.stop()
        self._redirect.__exit__(None, None, None)
        for p in reversed(self.patches):
            p.stop()
        video_store.reset_store_for_tests(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers -----------------------------------------------------------------
    def add_source(self, vid="aa0001", title="Clip"):
        (self.store.sources_dir / f"{vid}.mov").write_bytes(b"src")
        return self.store.add_video(source_file=f"{vid}.mov", title=title, sha256=vid, size_bytes=3,
                                    duration=20, width=1080, height=1920, added_by=1, video_id=vid)

    def creator(self, name="clipper"):
        return make_member(self.guild, name=name, roles=(creator_role_id(),))

    def fake_render(self, *, fail=None):
        def render(src, work_dir, *, seed, pitch_sign, siblings, ref, cancel):
            self.render_calls.append({"seed": seed, "pitch_sign": pitch_sign, "siblings": len(siblings), "src": src})
            if fail is not None:
                raise fail
            work_dir.mkdir(parents=True, exist_ok=True)
            out = work_dir / "attempt_0.mp4"
            out.write_bytes(b"rendered-" + ref.encode())
            params = sample_params("x", seed, "tweet_dark", 0, pitch_sign=pitch_sign)
            sig = pipeline.Signature(ref, [0.25, 0.5, 0.75], ["00" * 8] * 3, list(range(60)), pitch_sign, "tweet_dark")
            return pipeline.RenderResult(out, params, sig, {"elapsed_s": 0.1, "accepted_attempt": 0})

        return mock.patch.object(pipeline, "render_unique", side_effect=render)

    async def click(self, member):
        interaction = FakeInteraction(member, self.channel)
        view = content_generator.ContentGeneratorView()
        button = next(i for i in view.children if getattr(i, "custom_id", "") == "jelly_content_video")
        await button.callback(interaction)
        return interaction

    def thread_for(self, member):
        return next(t for t in self.channel.created if t.name == content_generator.thread_name_for(member))

    def deliveries(self, thread):
        return [m for m in thread.sent if m.get("view") is not None]


class ButtonTest(_Base):
    async def test_button_sits_next_to_the_other_two(self):
        view = content_generator.ContentGeneratorView()
        ids = [getattr(i, "custom_id", None) for i in view.children]
        self.assertEqual(ids, ["jelly_content_generate", "jelly_content_batch", "jelly_content_video"])
        video_button = view.children[2]
        self.assertEqual(video_button.label, "Vidéo")
        self.assertIn("**Vidéo**", content_generator.panel_embed().description)

    async def test_full_path_with_real_render_adapter(self):
        """Gate, thread, reservation, worker, real render_unique (ffmpeg calls stubbed), hosting, send."""
        self.add_source()
        member = self.creator("alice")

        def fake_encode(src, frame, params, dest, **kw):
            dest.write_bytes(b"mp4-bytes")

        frames = iter(np.random.default_rng(3).integers(0, 255, (40, CANVAS_H, CANVAS_W, 3), dtype=np.uint8))
        fps = {"src": np.random.default_rng(1).integers(0, 2**32, 300, dtype=np.uint64).astype(np.uint32),
               "out": np.random.default_rng(2).integers(0, 2**32, 300, dtype=np.uint64).astype(np.uint32)}
        with mock.patch.object(pipeline, "encode", side_effect=fake_encode), \
                mock.patch.object(media, "probe", side_effect=lambda p, **k: _probe(20 if p.suffix == ".mov" else 18)), \
                mock.patch.object(media, "fingerprint", side_effect=lambda p, **k: fps["src" if p.suffix == ".mov" else "out"]), \
                mock.patch.object(media, "grab_frame", side_effect=lambda *a, **k: next(frames)), \
                mock.patch.object(media, "output_problems", return_value=[]):
            interaction = await self.click(member)
            await video_generator.get_queue().drain()

        texts = interaction.texts()
        self.assertTrue(any("Demande reçue" in t and "1 à 3 min" in t for t in texts), texts)
        self.assertTrue(any("Ta vidéo est prête" in t for t in texts), texts)
        thread = self.thread_for(member)
        self.assertIn(member.id, thread.members)
        delivered = self.deliveries(thread)
        self.assertEqual(len(delivered), 1)
        url = view_urls(delivered[0]["view"])[0]
        token = url.rsplit("/", 1)[1]
        self.assertTrue(url.startswith("https://bot.example/v/"))
        self.assertNotIn(token, delivered[0]["content"])
        self.assertNotIn(token, self.stdout.getvalue())
        self.assertTrue((video_hosting.links_root() / token / "video.mp4").is_file())
        rows = [a for a in self.store._load()["assignments"]]
        self.assertEqual([r["status"] for r in rows], ["delivered"])
        self.assertEqual(len(self.store.sibling_signatures("aa0001")), 1)
        self.assertEqual(list(self.store.work_dir.iterdir()), [])

    async def test_three_clippers_queue_eta_and_alternating_siblings(self):
        self.add_source()
        members = [self.creator(f"c{i}") for i in range(3)]
        gate = asyncio.Event()
        real = video_generator.run_render

        async def slow_render(*args, **kwargs):
            await gate.wait()
            return await real(*args, **kwargs)

        with self.fake_render(), mock.patch.object(video_generator, "run_render", side_effect=slow_render):
            interactions = [await self.click(m) for m in members]
            await asyncio.sleep(0)
            gate.set()
            await video_generator.get_queue().drain()
        self.assertIn("Temps estimé : 1 à 3 min.", " ".join(interactions[0].texts()))
        self.assertIn("n°2", " ".join(interactions[1].texts()))
        self.assertIn("n°3", " ".join(interactions[2].texts()))
        self.assertIn("3 à 9 min", " ".join(interactions[2].texts()))
        signs = [c["pitch_sign"] for c in self.render_calls]
        self.assertEqual(signs, [signs[0], -signs[0], signs[0]])
        self.assertEqual([c["siblings"] for c in self.render_calls], [0, 1, 2])
        self.assertEqual(len({c["seed"] for c in self.render_calls}), 3)


class RefusalTest(_Base):
    async def test_non_creator_goes_nowhere(self):
        self.add_source()
        outsider = make_member(self.guild, name="outsider")
        interaction = await self.click(outsider)
        self.assertIn("créateur", interaction.response.messages[0]["content"])
        self.assertEqual(self.channel.created, [])
        self.assertEqual(self.store._load()["assignments"], [])

    async def test_duplicate_click_suppressed(self):
        self.add_source()
        self.add_source("aa0002")
        member = self.creator()
        gate = asyncio.Event()

        async def blocked(*a, **k):
            await gate.wait()
            raise media.MediaError("stop")

        with mock.patch.object(video_generator, "run_render", side_effect=blocked):
            await self.click(member)
            second = await self.click(member)
            self.assertIn("déjà en préparation", second.texts()[0])
            self.assertEqual(len(self.store._load()["assignments"]), 1)
            gate.set()
            await video_generator.get_queue().drain()
        self.assertEqual(self.store._load()["assignments"][0]["status"], "failed")
        # After the failure the creator can click again.
        with self.fake_render():
            await self.click(member)
            await video_generator.get_queue().drain()
        self.assertEqual(len(self.deliveries(self.thread_for(member))), 1)

    async def test_queue_full(self):
        self.add_source()
        gate = asyncio.Event()

        async def blocked(*a, **k):
            await gate.wait()
            raise media.MediaError("stop")

        with mock.patch.object(video_generator, "run_render", side_effect=blocked):
            for i in range(3):
                await self.click(self.creator(f"m{i}"))
            late = await self.click(self.creator("late"))
            self.assertIn("file vidéo est pleine", late.texts()[0])
            self.assertEqual(len(self.store._load()["assignments"]), 3)
            gate.set()
            await video_generator.get_queue().drain()

    async def test_thread_failure_consumes_nothing(self):
        self.add_source()
        self.channel = make_channel(self.guild, fail_create=True)
        member = self.creator()
        interaction = await self.click(member)
        self.assertIn("fil privé", interaction.texts()[-1])
        self.assertEqual(self.store._load()["assignments"], [])
        self.assertEqual(video_generator.get_queue().claim(member.id), "ok")

    async def test_public_url_missing_fails_before_anything(self):
        self.add_source()
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "", "RAILWAY_PUBLIC_DOMAIN": ""}), self.fake_render():
            interaction = await self.click(self.creator())
        self.assertIn("lien de téléchargement", interaction.texts()[0])
        self.assertEqual(self.channel.created, [])
        self.assertEqual(self.store._load()["assignments"], [])
        self.assertEqual(self.render_calls, [])

    async def test_missing_tools_disable_the_button(self):
        self.add_source()
        video_generator._selfcheck_problems = ["fpcalc introuvable"]
        interaction = await self.click(self.creator())
        self.assertIn("outils vidéo manquants", interaction.texts()[0])
        self.assertEqual(self.store._load()["assignments"], [])

    async def test_no_active_video(self):
        member = self.creator()
        interaction = await self.click(member)
        self.assertIn("Aucune vidéo", interaction.texts()[-1])
        self.assertEqual(video_generator.get_queue().claim(member.id), "ok")


class WorkerFailureTest(_Base):
    async def _one(self, *, render_fail=None, before_drain=None, send_fail=0):
        self.add_source()
        member = self.creator()
        gate = asyncio.Event()
        real = video_generator.run_render

        async def held(*args, **kwargs):
            await gate.wait()
            return await real(*args, **kwargs)

        with self.fake_render(fail=render_fail), mock.patch.object(video_generator, "run_render", side_effect=held):
            interaction = await self.click(member)
            thread = self.thread_for(member)
            thread.fail_sends = send_fail
            if before_drain:
                before_drain()
            gate.set()
            await video_generator.get_queue().drain()
        rows = self.store._load()["assignments"]
        return interaction, thread, rows

    async def test_guard_failure_releases(self):
        interaction, thread, rows = await self._one(render_fail=pipeline.GuardFailed({"attempts": []}))
        self.assertEqual((rows[0]["status"], rows[0]["reason"]), ("failed", "guard"))
        self.assertIn("4 essais", thread.sent[-1]["content"])
        self.assertEqual(list(video_hosting.links_root().iterdir()), [])

    async def test_media_error_releases(self):
        _i, thread, rows = await self._one(render_fail=media.MediaError("boom"))
        self.assertEqual(rows[0]["reason"], "media")
        self.assertNotIn("boom", thread.sent[-1]["content"])

    async def test_failed_send_rolls_back(self):
        # The thread welcome + note succeed; the delivery send fails.
        interaction, thread, rows = await self._one(send_fail=1)
        self.assertEqual((rows[0]["status"], rows[0]["reason"]), ("failed", "send_failed"))
        self.assertEqual(list(video_hosting.links_root().iterdir()), [])
        self.assertEqual(self.store.sibling_signatures("aa0001"), [])
        self.assertIn("Impossible d’écrire", interaction.texts()[-1])

    async def test_disabled_while_queued(self):
        _i, thread, rows = await self._one(before_drain=lambda: self.store.disable_video("aa0001", by=1))
        self.assertEqual(rows[0]["reason"], "disabled")
        self.assertEqual(self.render_calls, [])
        self.assertIn("retirée", thread.sent[-1]["content"])

    async def test_public_url_lost_after_enqueue(self):
        env = mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "", "RAILWAY_PUBLIC_DOMAIN": ""})
        try:
            _i, _thread, rows = await self._one(before_drain=env.start)
        finally:
            env.stop()
        self.assertEqual(rows[0]["reason"], "no_public_url")
        self.assertEqual(self.render_calls, [])

    async def test_worker_survives_a_crash(self):
        self.add_source()
        a, b = self.creator("a"), self.creator("b")
        calls = iter([RuntimeError("unexpected"), None])

        async def flaky(job, *args, **kwargs):
            error = next(calls)
            if error:
                raise error
            return await video_generator.__dict__["_real_run_render"](job, *args, **kwargs)

        video_generator._real_run_render = video_generator.run_render
        try:
            with self.fake_render(), mock.patch.object(video_generator, "run_render", side_effect=flaky):
                await self.click(a)
                await self.click(b)
                await video_generator.get_queue().drain()
        finally:
            del video_generator._real_run_render
        statuses = sorted(r["status"] for r in self.store._load()["assignments"])
        self.assertEqual(statuses, ["delivered", "failed"])

    async def test_stop_cancels_and_releases_everything(self):
        self.add_source()
        started = asyncio.Event()

        async def forever(job, *a, **k):
            started.set()
            await asyncio.sleep(3600)

        with mock.patch.object(video_generator, "run_render", side_effect=forever):
            await self.click(self.creator("a"))
            await self.click(self.creator("b"))
            await asyncio.wait_for(started.wait(), 5)
            running = video_generator.get_queue().running
            await video_generator.stop()
        self.assertTrue(running.cancel.is_set())
        rows = self.store._load()["assignments"]
        self.assertEqual({(r["status"], r["reason"]) for r in rows}, {("failed", "cancelled")})
        self.assertFalse(video_generator.get_queue().started)

    async def test_restart_recovery_notifies_thread(self):
        self.add_source()
        row = self.store.reserve(42, thread_id=777)
        await video_generator.stop()
        thread = mock.AsyncMock()
        bot = mock.Mock(get_channel=mock.Mock(return_value=thread))
        await video_generator.start(bot)
        self.assertEqual(self.store.get_assignment(row["id"])["status"], "failed")
        text = thread.send.await_args.args[0]
        self.assertIn("redémarrage", text)
        self.assertIn("<@42>", text)


class StaffCommandTest(_Base):
    def staff(self):
        return make_member(self.guild, name="staff", admin=True)

    async def run_add(self, member, attachment, title, probe=None):
        interaction = FakeInteraction(member, self.channel)
        with mock.patch.object(media, "ffprobe_bin", return_value="ffprobe"), \
                mock.patch.object(media, "probe", return_value=probe or _probe()):
            await video_generator.add_video_command.callback(interaction, video=attachment, titre=title)
        return interaction

    def source_file(self, name="clip.MOV", data=b"video-bytes"):
        path = self.tmp / name
        path.write_bytes(data)
        return path

    async def test_runtime_staff_gate(self):
        self.add_source()
        creator = self.creator()
        before = self.store.path.read_bytes()
        att = make_attachment(self.source_file())
        commands = [
            video_generator.add_video_command.callback(FakeInteraction(creator, self.channel), video=att, titre="x"),
            video_generator.list_videos_command.callback(FakeInteraction(creator, self.channel)),
            video_generator.disable_video_command.callback(FakeInteraction(creator, self.channel), video="aa0001"),
        ]
        for coro in commands:
            await coro
        self.assertEqual(self.store.path.read_bytes(), before)
        for command in (video_generator.add_video_command, video_generator.list_videos_command,
                        video_generator.disable_video_command):
            self.assertTrue(command.default_permissions.manage_guild)

    async def test_add_video_happy_path_and_escaped_title(self):
        interaction = await self.run_add(self.staff(), make_attachment(self.source_file()), "  @everyone <@123> **gras**\n ")
        text = interaction.texts()[-1]
        self.assertIn("Vidéo ajoutée", text)
        self.assertNotIn("@everyone", text.replace("@​everyone", ""))
        self.assertIn("\\*\\*gras\\*\\*", text)
        sent = interaction.followup.messages[-1]
        self.assertFalse(sent["allowed_mentions"].everyone)
        videos = self.store.list_videos()
        self.assertEqual(len(videos), 1)
        stored = self.store.source_path(videos[0])
        self.assertTrue(stored.is_file())
        self.assertRegex(stored.name, r"^[0-9a-f]{12}\.mov$")
        self.assertEqual([p.name for p in self.store.sources_dir.glob(".incoming-*")], [])
        again = await self.run_add(self.staff(), make_attachment(self.source_file()), "copie")
        self.assertIn("déjà active", again.texts()[-1])
        self.assertEqual(len(self.store.list_videos()), 1)

    async def test_add_video_rejections_leave_nothing(self):
        staff = self.staff()
        cases = [
            (make_attachment(self.source_file("a.txt"), content_type="text/plain"), "Clip", None, ".mp4"),
            (make_attachment(self.source_file(), size=10**12), "Clip", None, "maximum"),
            (make_attachment(self.source_file()), "   ", None, "titre"),
            (make_attachment(self.source_file()), "x" * 81, None, "titre"),
            (make_attachment(self.source_file()), "Clip", _probe(audio=False), "piste audio"),
            (make_attachment(self.source_file()), "Clip", _probe(duration=400), "maximum est 180"),
        ]
        for att, title, probe, needle in cases:
            interaction = await self.run_add(staff, att, title, probe)
            self.assertIn(needle, interaction.texts()[-1])
        self.assertEqual(self.store.list_videos(), [])
        self.assertEqual(list(self.store.sources_dir.iterdir()), [])

    async def test_list_and_disable(self):
        self.add_source("aa0001", "Premier")
        self.add_source("aa0002", "Second <@123456789012345678>")
        staff = self.staff()
        listing = FakeInteraction(staff, self.channel)
        await video_generator.list_videos_command.callback(listing)
        text = listing.texts()[0]
        self.assertIn("2 actives / 2", text)
        self.assertIn("aa0002", text)
        self.assertNotIn("<@123456789012345678>", text)
        disable = FakeInteraction(staff, self.channel)
        await video_generator.disable_video_command.callback(disable, video="aa0001")
        self.assertIn("désactivée", disable.texts()[0])
        self.assertFalse(self.store.get_video("aa0001")["active"])
        again = FakeInteraction(staff, self.channel)
        await video_generator.disable_video_command.callback(again, video="aa0001")
        self.assertIn("déjà désactivée", again.texts()[0])
        bad = FakeInteraction(staff, self.channel)
        await video_generator.disable_video_command.callback(bad, video="../../x")
        self.assertIn("invalide", bad.texts()[0])
        choices = await video_generator._disable_autocomplete(FakeInteraction(staff, self.channel), "sec")
        self.assertEqual([c.value for c in choices], ["aa0002"])


class CopyTest(unittest.TestCase):
    def test_new_french_copy_has_no_long_dashes(self):
        for name in ("video_generator.py", "video_hosting.py", "video_store.py"):
            source = (REPO / name).read_text(encoding="utf-8")
            for dash in LONG_DASHES:
                self.assertNotIn(dash, source, name)
        line = next(l for l in (REPO / "content_generator.py").read_text(encoding="utf-8").splitlines() if "**Vidéo**" in l)
        for dash in LONG_DASHES:
            self.assertNotIn(dash, line)

    def test_eta(self):
        self.assertEqual(video_generator.eta_message(0), "Temps estimé : 1 à 3 min.")
        self.assertIn("n°2 dans la file (1 vidéo avant toi)", video_generator.eta_message(1))
        self.assertIn("4 à 12 min", video_generator.eta_message(3))

    def test_clean_title(self):
        self.assertEqual(video_generator.clean_title(" a​\x00  b "), "a b")
        self.assertIsNone(video_generator.clean_title("\n\t"))


if __name__ == "__main__":
    unittest.main()
