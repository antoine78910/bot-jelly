"""video_hosting: /v/<token> routes, expiry, traversal, atomic publish, restart safety."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import video_hosting
import video_store
from video_store import VideoStore


class HostingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        video_store.reset_store_for_tests(VideoStore(self.tmp / "data"))
        self.env = mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://bot.example"})
        self.env.start()
        self.video = self.tmp / "out.mp4"
        self.video.write_bytes(b"\x00" * 4096)
        self.client = await self._client()

    async def _client(self):
        app = web.Application()
        video_hosting.register_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        return client

    async def asyncTearDown(self):
        await self.client.close()
        self.env.stop()
        video_store.reset_store_for_tests(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_publish_page_and_download(self):
        token, url, expires = video_hosting.publish_video(self.video)
        self.assertEqual(url, f"https://bot.example/v/{token}")
        self.assertGreater(len(token), 40)
        self.assertAlmostEqual(expires - time.time(), 7 * 24 * 3600, delta=5)
        page = await self.client.get(f"/v/{token}")
        self.assertEqual(page.status, 200)
        self.assertIn("Télécharger la vidéo", await page.text())
        self.assertIn("noindex", page.headers["X-Robots-Tag"])
        inline = await self.client.get(f"/v/{token}/video.mp4")
        self.assertEqual(inline.status, 200)
        self.assertTrue(inline.headers["Content-Disposition"].startswith("inline"))
        self.assertEqual(await inline.read(), self.video.read_bytes())
        dl = await self.client.get(f"/v/{token}/video.mp4?dl=1")
        self.assertRegex(dl.headers["Content-Disposition"], r'^attachment; filename="IMG_\d{4}\.mp4"$')
        ranged = await self.client.get(f"/v/{token}/video.mp4", headers={"Range": "bytes=0-9"})
        self.assertEqual(ranged.status, 206)

    async def test_unknown_expired_traversal_and_listing(self):
        token, _url, _exp = video_hosting.publish_video(self.video, ttl=-1)
        resp = await self.client.get(f"/v/{token}")
        self.assertEqual(resp.status, 410)
        self.assertFalse((video_hosting.links_root() / token).exists())
        self.assertEqual((await self.client.get(f"/v/{token}")).status, 404)
        bad = ["A" * 43, "..%2F..%2Fdata", "%2e%2e", "manifest.json", ".tmp-" + "a" * 40]
        for token_like in bad:
            self.assertEqual((await self.client.get(f"/v/{token_like}")).status, 404, token_like)
            self.assertEqual((await self.client.get(f"/v/{token_like}/video.mp4")).status, 404, token_like)
        for path in ("/v", "/v/", "/v/x/manifest.json"):
            self.assertEqual((await self.client.get(path)).status, 404, path)

    async def test_restart_safe_tokens(self):
        token, _url, _exp = video_hosting.publish_video(self.video)
        await self.client.close()
        video_store.reset_store_for_tests(VideoStore(self.tmp / "data"))
        self.client = await self._client()
        self.assertEqual((await self.client.get(f"/v/{token}/video.mp4")).status, 200)

    async def test_atomic_publish_and_cleanup(self):
        with mock.patch("shutil.copyfile", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                video_hosting.publish_video(self.video)
        self.assertEqual(list(video_hosting.links_root().iterdir()), [])
        live, _u, _e = video_hosting.publish_video(self.video)
        dead, _u, _e = video_hosting.publish_video(self.video, ttl=-5)
        stale = video_hosting.links_root() / ".tmp-old"
        stale.mkdir()
        os.utime(stale, (time.time() - 7200, time.time() - 7200))
        self.assertEqual(video_hosting.cleanup_expired(), 1)
        names = {p.name for p in video_hosting.links_root().iterdir()}
        self.assertEqual(names, {live})
        video_hosting.unpublish(live)
        video_hosting.unpublish("../../etc")
        self.assertEqual(list(video_hosting.links_root().iterdir()), [])

    async def test_public_url_required(self):
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "", "RAILWAY_PUBLIC_DOMAIN": ""}):
            self.assertFalse(video_hosting.hosting_ready())
            with self.assertRaises(RuntimeError):
                video_hosting.publish_video(self.video)
        self.assertEqual(list(video_hosting.links_root().iterdir()), [])

    def test_access_log_redacts_tokens(self):
        self.assertEqual(video_hosting.redact_path("/v/abcDEF_123-xyz/video.mp4"), "/v/<token>/video.mp4")
        self.assertEqual(video_hosting.redact_path("/c/tok"), "/c/<token>")
        self.assertEqual(video_hosting.redact_path("/health"), "/health")


if __name__ == "__main__":
    unittest.main()
