"""video_uniquify: params, frames, metrics, guards, retries, subprocess safety."""

from __future__ import annotations

import os
import random
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from video_uniquify import media, metrics, pipeline
from video_uniquify.chrome import INTER_PATH
from video_uniquify.frames import (
    CANVAS_H,
    CANVAS_W,
    HOOKS,
    QUOTE_REACTIONS,
    TEMPLATES,
    WIDTH_MAX,
    WIDTH_MIN,
    render_frame,
    sample_params,
)

LONG_DASHES = ("—", "–", "―")


def _probe(duration=12.0, *, audio=True, width=1080, height=1920, rotation=None, tags=None):
    video = {"codec_type": "video", "codec_name": "h264", "width": width, "height": height,
             "r_frame_rate": "60/1", "duration": str(duration)}
    if rotation is not None:
        video["side_data_list"] = [{"rotation": rotation}]
    streams = [video]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000"})
    return {"streams": streams, "format": {"duration": str(duration), "tags": tags or {}}}


class ParamsTest(unittest.TestCase):
    def test_ranges_and_forced_pitch_sign(self):
        for seed in range(300):
            for template in TEMPLATES:
                sign = 1 if seed % 2 else -1
                p = sample_params("x", seed * 7919 + 13, template, 0, pitch_sign=sign)
                self.assertTrue(1.04 <= p.speed <= 1.15)
                self.assertTrue(1.0 <= abs(p.semitones) <= 2.0)
                self.assertEqual(p.semitones > 0, sign > 0)
                self.assertTrue(WIDTH_MIN - 0.002 <= p.width_frac <= WIDTH_MAX + 0.002, p.width_frac)
                self.assertTrue(p.width_frac >= 0.55 - 0.002)  # prototype allowed 0.54 / 0.52

    def test_deterministic_and_rejects_zero_sign(self):
        a = sample_params("a", 42, "tweet_dark", 0, pitch_sign=1)
        b = sample_params("b", 42, "tweet_dark", 0, pitch_sign=1)
        self.assertEqual((a.speed, a.semitones, a.video_x, a.views), (b.speed, b.semitones, b.video_x, b.views))
        with self.assertRaises(ValueError):
            sample_params("a", 42, "tweet_dark", 0, pitch_sign=0)

    def test_retry_seeds_differ_and_keep_sign(self):
        seeds = {pipeline.attempt_seed(123, k) for k in range(pipeline.MAX_ATTEMPTS)}
        self.assertEqual(len(seeds), pipeline.MAX_ATTEMPTS)
        self.assertEqual(pipeline.MAX_ATTEMPTS, 4)

    def test_french_copy_has_no_long_dashes(self):
        for text in list(HOOKS.values()) + QUOTE_REACTIONS:
            for dash in LONG_DASHES:
                self.assertNotIn(dash, text)


class FrameTest(unittest.TestCase):
    def test_font_is_vendored_with_license(self):
        self.assertTrue(INTER_PATH.is_file())
        self.assertTrue((INTER_PATH.parent / "OFL.txt").is_file())
        self.assertIn("video_uniquify", str(INTER_PATH))

    def test_render_every_template_with_transparent_window(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            for i, template in enumerate(TEMPLATES):
                p = sample_params("x", 1000 + i, template, 0, pitch_sign=-1)
                path = render_frame(p, Path(tmp) / f"{template}.png")
                im = Image.open(path)
                self.assertEqual(im.size, (CANVAS_W, CANVAS_H))
                center = im.getpixel((p.video_x + p.video_w // 2, p.video_y + p.video_h // 2))
                self.assertEqual(center[3], 0, template)
                self.assertTrue(p.video_y + p.video_h <= CANVAS_H)
                self.assertTrue(p.video_x + p.video_w <= CANVAS_W)


class MetricsTest(unittest.TestCase):
    def test_chromaprint_identity_random_and_fail_closed(self):
        rng = np.random.default_rng(1)
        a = rng.integers(0, 2**32, size=300, dtype=np.uint64).astype(np.uint32)
        b = rng.integers(0, 2**32, size=300, dtype=np.uint64).astype(np.uint32)
        self.assertAlmostEqual(metrics.chromaprint_similarity(a, a), 1.0)
        self.assertLess(metrics.chromaprint_similarity(a, b), 0.15)
        self.assertIsNone(metrics.chromaprint_similarity(a[:10], a[:10]))
        self.assertIsNone(metrics.chromaprint_similarity(np.array([], np.uint32), a))

    def test_phash_hex_roundtrip(self):
        img = np.random.default_rng(2).integers(0, 255, (64, 36, 3), dtype=np.uint8)
        bits = metrics.phash_bits(img)
        self.assertEqual(bits.size, 64)
        self.assertTrue(np.array_equal(metrics.hex_to_bits(metrics.bits_to_hex(bits)), bits))
        self.assertEqual(metrics.phash_dist(img, img), 0)
        with self.assertRaises(ValueError):
            metrics.hex_to_bits("abcd")

    def test_check_fails_closed_on_nan_none_inf(self):
        for bad in (None, float("nan"), float("inf")):
            self.assertFalse(pipeline._check("x", bad, 0.2, "<=")["ok"])
        self.assertTrue(pipeline._check("x", 0.1, 0.2, "<=")["ok"])
        self.assertFalse(pipeline._check("x", 15.9, 16, ">=")["ok"])


class ProbeTest(unittest.TestCase):
    def test_source_validation(self):
        info = media.source_info(_probe(30))
        self.assertEqual((info.width, info.height), (1080, 1920))
        rotated = media.source_info(_probe(30, width=1920, height=1080, rotation=-90))
        self.assertEqual((rotated.width, rotated.height), (1080, 1920))
        cases = [
            (_probe(30, audio=False), "audio"),
            (_probe(181), "maximum"),
            (_probe(2), "minimum"),
            (_probe(float("nan")), "illisible"),
            (_probe(30, width=1920, height=1080), "9:16"),
            ({"streams": [], "format": {}}, "piste vidéo"),
        ]
        for data, needle in cases:
            with self.assertRaises(media.MediaError) as ctx:
                media.source_info(data)
            self.assertIn(needle, str(ctx.exception))

    def test_output_problems(self):
        self.assertEqual(media.output_problems(_probe(10)), [])
        self.assertTrue(media.output_problems(_probe(10, tags={"creation_time": "x"})))
        self.assertTrue(media.output_problems(_probe(10, audio=False)))
        self.assertTrue(media.output_problems(_probe(10, width=720)))


class SubprocessTest(unittest.TestCase):
    def test_timeout_kills(self):
        started = time.monotonic()
        with self.assertRaises(media.MediaError):
            media.run_cmd([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        self.assertLess(time.monotonic() - started, 15)

    def test_cancel_kills(self):
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        with self.assertRaises(media.RenderCancelled):
            media.run_cmd([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60, cancel=cancel)

    def test_selfcheck_reports_every_missing_tool(self):
        with mock.patch.dict(os.environ, {"FFMPEG_PATH": "", "FFPROBE_PATH": "", "FPCALC_PATH": ""}), \
                mock.patch("shutil.which", return_value=None):
            problems = " ".join(media.selfcheck())
        for tool in ("ffmpeg", "ffprobe", "fpcalc"):
            self.assertIn(tool, problems)

    def test_selfcheck_requires_rubberband_filter(self):
        fake = mock.Mock(stdout=" ... scale   V->V  Scale\n V..... libx264  H.264\n A..... aac  AAC\n")
        with mock.patch.object(media, "ffmpeg_bin", return_value="ffmpeg"), \
                mock.patch.object(media, "ffprobe_bin", return_value="ffprobe"), \
                mock.patch.object(media, "fpcalc_bin", return_value="fpcalc"), \
                mock.patch("subprocess.run", return_value=fake):
            self.assertTrue(any("rubberband" in p for p in media.selfcheck()))


class EncodeCommandTest(unittest.TestCase):
    def test_filter_and_command(self):
        p = sample_params("x", 7, "tweet_light", 0, pitch_sign=1)
        graph = pipeline.build_filter(p)
        self.assertIn("rubberband=tempo=", graph)
        self.assertNotIn("asetrate", graph)
        self.assertNotIn("duration=30", graph)
        with mock.patch.object(media, "ffmpeg_bin", return_value="ffmpeg"):
            cmd = pipeline.encode_command(Path("in.mov"), Path("f.png"), p, Path("o.mp4"), 2)
        joined = " ".join(cmd)
        for part in ("-map_metadata -1", "-crf 20", "-r 60", "-b:a 192k", "-ar 48000", "libx264", "-threads 2", "-nostdin"):
            self.assertIn(part, joined)

    def test_threads_env_is_capped(self):
        with mock.patch.dict(os.environ, {"VIDEO_FFMPEG_THREADS": "64"}):
            self.assertEqual(pipeline.ffmpeg_threads(), 8)
        with mock.patch.dict(os.environ, {"VIDEO_FFMPEG_THREADS": ""}):
            self.assertEqual(pipeline.ffmpeg_threads(), 2)


def _rand_fp(seed, n=200):
    return np.random.default_rng(seed).integers(0, 2**32, size=n, dtype=np.uint64).astype(np.uint32)


class RenderUniqueTest(unittest.TestCase):
    """Real orchestration with the ffmpeg-touching calls replaced."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src.mov"
        self.src.write_bytes(b"src")
        self.encoded: list[Path] = []
        self.sample_calls: list[dict] = []

        def fake_encode(src, frame, params, dest, **kw):
            self.assertTrue(frame.is_file())
            dest.write_bytes(b"mp4")
            self.encoded.append(dest)

        def fake_probe(path, **kw):
            return _probe(12.0) if path == self.src else _probe(10.5)

        real_sample = pipeline.sample_params

        def spy_sample(*args, **kwargs):
            params = real_sample(*args, **kwargs)
            self.sample_calls.append({"sign": kwargs["pitch_sign"], "semitones": params.semitones})
            return params

        self.patches = [
            mock.patch.object(media, "selfcheck", return_value=[]),
            mock.patch.object(media, "probe", side_effect=fake_probe),
            mock.patch.object(media, "fingerprint", side_effect=lambda p, **kw: _rand_fp(len(str(p)))),
            mock.patch.object(media, "grab_frame", return_value=np.zeros((CANVAS_H, CANVAS_W, 3), np.uint8)),
            mock.patch.object(pipeline, "encode", side_effect=fake_encode),
            mock.patch.object(pipeline, "sample_params", side_effect=spy_sample),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _evaluate_sequence(self, oks):
        results = iter(oks)

        def fake_eval(*args, **kwargs):
            ok = next(results)
            return {"ok": ok, "checks": [{"name": "x", "ok": ok}], "visual_samples": [], "sibling_samples": [], "siblings_compared": len(kwargs["siblings"])}

        return mock.patch.object(pipeline, "evaluate", side_effect=fake_eval)

    def test_passes_on_last_retry_and_deletes_rejected(self):
        work = self.tmp / "work"
        with self._evaluate_sequence([False, False, False, True]):
            result = pipeline.render_unique(self.src, work, seed=99, pitch_sign=-1)
        self.assertEqual(result.report["accepted_attempt"], 3)
        self.assertEqual(len(self.encoded), 4)
        self.assertEqual(sorted(p.name for p in work.iterdir()), ["attempt_3.mp4"])
        self.assertTrue(all(c["sign"] == -1 and c["semitones"] < 0 for c in self.sample_calls))
        self.assertEqual(result.signature.pitch_sign, -1)
        self.assertEqual(len(result.signature.phash), len(pipeline.SIBLING_SOURCE_FRACS))
        templates = {a["params"]["template"] for a in result.report["attempts"]}
        self.assertGreater(len(templates), 1)

    def test_exhausted_fails_safe_without_leaving_files(self):
        work = self.tmp / "work"
        with self._evaluate_sequence([False] * 4):
            with self.assertRaises(pipeline.GuardFailed) as ctx:
                pipeline.render_unique(self.src, work, seed=5, pitch_sign=1)
        self.assertEqual(len(ctx.exception.report["attempts"]), 4)
        self.assertEqual(list(work.iterdir()), [])
        self.assertTrue(all(c["sign"] == 1 for c in self.sample_calls))

    def test_missing_tools_fail_before_encoding(self):
        with mock.patch.object(media, "selfcheck", return_value=["fpcalc introuvable"]):
            with self.assertRaises(media.MediaError):
                pipeline.render_unique(self.src, self.tmp / "w", seed=1, pitch_sign=1)
        self.assertEqual(self.encoded, [])

    def test_short_source_fingerprint_fails_closed(self):
        with mock.patch.object(media, "fingerprint", return_value=_rand_fp(1, n=10)):
            with self.assertRaises(media.MediaError):
                pipeline.render_unique(self.src, self.tmp / "w", seed=1, pitch_sign=1)
        self.assertEqual(self.encoded, [])

    def test_cancel_removes_partial_output(self):
        work = self.tmp / "work"

        def cancelled_encode(src, frame, params, dest, **kw):
            dest.write_bytes(b"partial")
            raise media.RenderCancelled("ffmpeg")

        with mock.patch.object(pipeline, "encode", side_effect=cancelled_encode):
            with self.assertRaises(media.RenderCancelled):
                pipeline.render_unique(self.src, work, seed=1, pitch_sign=1)
        self.assertEqual(list(work.iterdir()), [])


class EvaluateTest(unittest.TestCase):
    def _img(self, seed):
        return np.random.default_rng(seed).integers(0, 255, (CANVAS_H, CANVAS_W, 3), dtype=np.uint8)

    def test_identical_frames_and_audio_fail_every_original_guard(self):
        p = sample_params("x", 3, "tweet_dark", 0, pitch_sign=1)
        fp = _rand_fp(9)
        frame = self._img(1)
        with mock.patch.object(media, "grab_frame", return_value=frame):
            result = pipeline.evaluate(Path("a"), Path("b"), p, src_duration=20, var_duration=18,
                                       src_fp=fp, var_fp=fp, var_bits=[], siblings=[])
        self.assertFalse(result["ok"])
        self.assertTrue(all(not c["ok"] for c in result["checks"]))

    def test_every_sibling_is_checked(self):
        p = sample_params("x", 3, "tweet_dark", 0, pitch_sign=1)
        var_fp = _rand_fp(10)
        var_bits = [metrics.phash_bits(self._img(i)) for i in range(3)]
        far = pipeline.Signature("far", list(pipeline.SIBLING_SOURCE_FRACS),
                                 [metrics.bits_to_hex(~b) for b in var_bits], _rand_fp(11).tolist())
        same_audio = pipeline.Signature("same_audio", list(pipeline.SIBLING_SOURCE_FRACS),
                                        [metrics.bits_to_hex(~b) for b in var_bits], var_fp.tolist())
        same_look = pipeline.Signature("same_look", list(pipeline.SIBLING_SOURCE_FRACS),
                                       [metrics.bits_to_hex(b) for b in var_bits], _rand_fp(12).tolist())
        frames = iter([self._img(100 + i) for i in range(10)])
        with mock.patch.object(media, "grab_frame", side_effect=lambda *a, **k: next(frames)):
            result = pipeline.evaluate(Path("a"), Path("b"), p, src_duration=20, var_duration=18,
                                       src_fp=_rand_fp(13), var_fp=var_fp, var_bits=var_bits,
                                       siblings=[far, same_audio, same_look])
        failed = {(c["name"], c.get("sibling")) for c in result["checks"] if not c["ok"]}
        self.assertEqual(failed, {("chromaprint_vs_sibling", "same_audio"), ("phash_mean_vs_sibling", "same_look")})
        self.assertEqual(result["siblings_compared"], 3)

    def test_sibling_frames_aligned_on_source_time(self):
        p = sample_params("x", 3, "tweet_dark", 0, pitch_sign=1)
        times = []
        with mock.patch.object(media, "grab_frame", side_effect=lambda path, t, **k: times.append(t) or self._img(0)):
            pipeline.sibling_bits(Path("v"), p, 30.0, 30.0 / p.speed, None)
        expected = [(f * 30.0 - p.trim) / p.speed for f in pipeline.SIBLING_SOURCE_FRACS]
        for got, want in zip(times, expected):
            self.assertAlmostEqual(got, want, places=6)

    def test_signature_roundtrip_and_validation(self):
        sig = pipeline.Signature("r", [0.5], ["00" * 8], [1, 2, 3], 1, "quote_dark")
        self.assertEqual(pipeline.Signature.from_json(sig.to_json()), sig)
        with self.assertRaises(ValueError):
            pipeline.Signature.from_json({**sig.to_json(), "phash": ["zz"]})


@unittest.skipUnless(
    os.getenv("VIDEO_E2E") == "1" and not media.selfcheck(),
    "set VIDEO_E2E=1 on a box with ffmpeg+rubberband, ffprobe and fpcalc",
)
class RealEncodeTest(unittest.TestCase):
    def test_real_render(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            src = tmp / "src.mp4"
            code, _o, err = media.run_cmd([
                media.ffmpeg_bin(), "-nostdin", "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=8",
                "-f", "lavfi", "-i", "anoisesrc=d=8:c=pink:r=44100",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src),
            ], timeout=120)
            self.assertEqual(code, 0, err)
            try:
                result = pipeline.render_unique(src, tmp / "w", seed=random.randrange(2**32), pitch_sign=1)
            except pipeline.GuardFailed as exc:
                self.fail(f"guards failed: {exc.report}")
            self.assertEqual(media.output_problems(media.probe(result.output)), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
