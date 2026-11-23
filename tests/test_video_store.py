"""video_store: rotation, cycles, reservations, pitch alternation, restart, corruption, housekeeping."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import video_store
from video_store import AlreadyReserved, NoActiveVideo, StoreUnavailable, VideoStore

DAY = 24 * 3600


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = VideoStore(self.tmp)
        self.now = 1_800_000_000.0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, vid, *, at=None):
        (self.store.sources_dir / f"{vid}.mov").write_bytes(b"x")
        return self.store.add_video(
            source_file=f"{vid}.mov", title=f"T {vid}", sha256=vid, size_bytes=1, duration=30,
            width=1080, height=1920, added_by=1, video_id=vid, now=at or self.now,
        )

    def tick(self, seconds=1.0):
        self.now += seconds
        return self.now

    def deliver(self, assignment, sig=None):
        return self.store.mark_delivered(
            assignment["id"], signature=sig or {"ref": assignment["id"]},
            expires_at=self.now + video_store.LINK_TTL_S, now=self.tick(),
        )

    def take(self, clipper):
        a = self.store.reserve(clipper, now=self.tick())
        self.deliver(a)
        return a


class RotationTest(_Base):
    def test_no_repeat_until_cycle_complete_then_new_cycle(self):
        for i, vid in enumerate(("aa01", "aa02", "aa03")):
            self.add(vid, at=self.now + i)
        first = [self.take(7) for _ in range(3)]
        self.assertEqual(len({a["video_id"] for a in first}), 3)
        self.assertEqual({a["cycle"] for a in first}, {0})
        fourth = self.take(7)
        self.assertEqual(fourth["cycle"], 1)
        self.assertEqual(fourth["video_id"], first[0]["video_id"])  # least recently assigned

    def test_least_recently_assigned_is_global(self):
        self.add("aa01", at=self.now)
        self.add("aa02", at=self.now + 1)
        a = self.take(1)
        b = self.take(2)
        self.assertNotEqual(a["video_id"], b["video_id"])

    def test_disabled_never_assigned_and_new_video_joins_cycle(self):
        self.add("aa01")
        self.add("aa02", at=self.now + 1)
        self.store.disable_video("aa01", by=5)
        self.assertEqual(self.take(1)["video_id"], "aa02")
        self.add("aa03", at=self.now + 2)
        self.assertEqual(self.take(1)["video_id"], "aa03")
        self.store.disable_video("aa02", by=5)
        self.store.disable_video("aa03", by=5)
        with self.assertRaises(NoActiveVideo):
            self.store.reserve(1, now=self.tick())

    def test_missing_source_file_is_not_assigned(self):
        self.add("aa01")
        (self.store.sources_dir / "aa01.mov").unlink()
        with self.assertRaises(NoActiveVideo):
            self.store.reserve(1)

    def test_one_reservation_per_clipper(self):
        self.add("aa01")
        self.add("aa02")
        self.store.reserve(1, now=self.tick())
        with self.assertRaises(AlreadyReserved):
            self.store.reserve(1, now=self.tick())

    def test_release_frees_the_video_and_restores_lra(self):
        self.add("aa01")
        self.add("aa02", at=self.now + 1)
        a = self.store.reserve(1, now=self.tick())
        self.assertTrue(self.store.release(a["id"], "guard"))
        self.assertFalse(self.store.release(a["id"], "again"))
        self.assertIsNone(self.store.get_video(a["video_id"])["last_assigned_at"])
        b = self.store.reserve(1, now=self.tick())
        self.assertEqual(b["video_id"], a["video_id"])
        self.assertEqual(b["cycle"], 0)

    def test_delivered_requires_reserved(self):
        self.add("aa01")
        a = self.store.reserve(1, now=self.tick())
        self.store.release(a["id"], "x")
        with self.assertRaises(StoreUnavailable):
            self.deliver(a)


class SeedAndPitchTest(_Base):
    def test_seed_stable_per_clipper_video_cycle_and_salted(self):
        self.add("aa01")
        salt = json.loads(self.store.path.read_text())["salt"]
        s1 = self.store.seed_for(salt, 1, "aa01", 0)
        self.assertEqual(s1, self.store.seed_for(salt, 1, "aa01", 0))
        self.assertNotEqual(s1, self.store.seed_for(salt, 2, "aa01", 0))
        self.assertNotEqual(s1, self.store.seed_for(salt, 1, "aa01", 1))
        self.assertNotEqual(s1, self.store.seed_for("00" * 32, 1, "aa01", 0))
        a = self.store.reserve(1, now=self.tick())
        self.assertEqual(a["seed"], s1)

    def test_salt_never_printed(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.add("aa01")
            self.store.reserve(1)
            self.store.housekeeping()
        salt = json.loads(self.store.path.read_text())["salt"]
        self.assertNotIn(salt, out.getvalue())

    def test_siblings_alternate_pitch_sign_even_after_failures(self):
        self.add("aa01")
        signs = []
        for clipper in range(1, 6):
            a = self.store.reserve(clipper, now=self.tick())
            if clipper == 3:  # failed render: its sign must not count
                self.store.release(a["id"], "guard")
                a = self.store.reserve(clipper, now=self.tick())
            signs.append(a["pitch_sign"])
            self.deliver(a)
        for prev, cur in zip(signs, signs[1:]):
            self.assertEqual(cur, -prev)

    def test_concurrent_reservations_of_same_source_alternate(self):
        self.add("aa01")
        a = self.store.reserve(1, now=self.tick())
        b = self.store.reserve(2, now=self.tick())
        self.assertEqual(b["pitch_sign"], -a["pitch_sign"])


class RestartAndCorruptionTest(_Base):
    def test_restart_frees_reservations_and_work(self):
        self.add("aa01")
        a = self.store.reserve(1, thread_id=55, now=self.tick())
        (self.store.work_dir / "x").mkdir()
        again = VideoStore(self.tmp)
        rows = again.recover_after_restart()
        self.assertEqual([(r["id"], r["thread_id"]) for r in rows], [(a["id"], 55)])
        self.assertEqual(again.get_assignment(a["id"])["status"], "failed")
        self.assertEqual(list(again.work_dir.iterdir()), [])
        self.assertEqual(again.reserve(1)["video_id"], "aa01")

    def test_corrupt_store_is_refused_and_left_untouched(self):
        self.add("aa01")
        self.store.path.write_text("{not json", encoding="utf-8")
        for call in (self.store.list_videos, lambda: self.store.reserve(1), self.store.housekeeping):
            with self.assertRaises(StoreUnavailable):
                call()
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), "{not json")
        self.store.path.write_text(json.dumps({"videos": [], "assignments": []}), encoding="utf-8")
        with self.assertRaises(StoreUnavailable):
            self.store.list_videos()

    def test_missing_store_with_sources_is_refused(self):
        self.add("aa01")
        self.store.path.unlink()
        with self.assertRaises(StoreUnavailable):
            self.store.list_videos()

    def test_atomic_write_leaves_no_temp(self):
        self.add("aa01")
        self.store.reserve(1)
        leftovers = [p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_unreadable_sibling_signature_blocks(self):
        self.add("aa01")
        a = self.store.reserve(1, now=self.tick())
        self.deliver(a, {"ref": a["id"], "x": 1})
        self.assertEqual(self.store.sibling_signatures("aa01"), [{"ref": a["id"], "x": 1}])
        (self.tmp / "signatures" / f"{a['id']}.json").write_text("nope")
        with self.assertRaises(StoreUnavailable):
            self.store.sibling_signatures("aa01")


class HousekeepingTest(_Base):
    def test_prunes_old_failures_keeps_recent(self):
        self.add("aa01")
        a = self.store.reserve(1, now=self.tick())
        self.store.release(a["id"], "x", now=self.now)
        self.store.housekeeping(now=self.now + DAY)
        self.assertIsNotNone(self.store.get_assignment(a["id"]))
        self.store.housekeeping(now=self.now + 8 * DAY)
        self.assertIsNone(self.store.get_assignment(a["id"]))

    def test_signatures_bounded_per_source(self):
        self.add("aa01")
        with mock.patch.object(video_store, "SIBLING_KEEP", 3):
            for clipper in range(5):
                self.take(clipper)
            stats = self.store.housekeeping(now=self.now)
            self.assertEqual(stats["signatures_pruned"], 2)
            self.assertEqual(len(self.store.sibling_signatures("aa01")), 3)
        self.assertEqual(len(list((self.tmp / "signatures").iterdir())), 3)

    def test_old_cycles_pruned_only_after_link_expiry(self):
        self.add("aa01")
        with mock.patch.object(video_store, "SIBLING_KEEP", 0):
            first = self.take(1)
            second = self.take(1)  # cycle 1
            self.assertEqual(second["cycle"], 1)
            self.store.housekeeping(now=self.now)
            self.assertIsNotNone(self.store.get_assignment(first["id"]))
            self.store.housekeeping(now=self.now + 9 * DAY)
            self.assertIsNone(self.store.get_assignment(first["id"]))
            self.assertIsNotNone(self.store.get_assignment(second["id"]))
        self.assertEqual(self.store.reserve(1, now=self.tick())["cycle"], 2)

    def test_disabled_source_deleted_only_when_idle(self):
        self.add("aa01")
        a = self.store.reserve(1, now=self.tick())
        self.store.disable_video("aa01", by=2)
        self.store.housekeeping()
        self.assertTrue((self.store.sources_dir / "aa01.mov").exists())
        self.store.release(a["id"], "disabled")
        self.store.housekeeping()
        self.assertFalse((self.store.sources_dir / "aa01.mov").exists())
        self.assertTrue(self.store.get_video("aa01")["source_deleted"])

    def test_stale_work_and_incoming_removed(self):
        work = self.store.work_dir / "old"
        work.mkdir()
        incoming = self.store.sources_dir / ".incoming-abc.mov"
        incoming.write_bytes(b"x")
        os.utime(work, (self.now - DAY, self.now - DAY))
        os.utime(incoming, (self.now - DAY, self.now - DAY))
        self.store.housekeeping(now=self.now)
        self.assertFalse(work.exists())
        self.assertFalse(incoming.exists())


class DataDirTest(unittest.TestCase):
    def test_fallback_when_not_writable_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("x")
            fallback = Path(tmp) / "fallback"
            with mock.patch.dict(os.environ, {"DATA_DIR": str(blocker / "data")}), \
                    mock.patch.object(video_store, "LOCAL_FALLBACK", fallback):
                path, persistent, warning = video_store.resolve_data_dir()
        self.assertEqual(path, fallback)
        self.assertFalse(persistent)
        self.assertIn("NOT persistent", warning)

    def test_railway_without_volume_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"DATA_DIR": tmp, "RAILWAY_PROJECT_ID": "p", "RAILWAY_VOLUME_MOUNT_PATH": ""}
            with mock.patch.dict(os.environ, env):
                _path, persistent, warning = video_store.resolve_data_dir()
            self.assertFalse(persistent)
            self.assertIn("no Railway volume", warning)
            with mock.patch.dict(os.environ, {**env, "RAILWAY_VOLUME_MOUNT_PATH": tmp}):
                _path, persistent, warning = video_store.resolve_data_dir()
            self.assertTrue(persistent)
            self.assertEqual(warning, "")


if __name__ == "__main__":
    unittest.main()
