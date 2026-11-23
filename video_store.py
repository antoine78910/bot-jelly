"""JSON store for the Vidéo button: source clips, assignments, sibling signatures.

Layout under DATA_DIR (default /data, a Railway volume in production):
  top-videos.json       {"version", "salt", "videos": [...], "assignments": [...]}
  sources/<id>.<ext>    validated source clips (never the uploader's file name)
  signatures/<id>.json  per delivered assignment: pHash + chromaprint for sibling guards
  links/<token>/        hosted downloads (video_hosting.py)
  work/<id>/            render scratch space, deleted after each job

Single replica only: one process owns the file. Every change is load, modify,
atomic replace under one lock, so reservations and deliveries are serialized.

Rotation: among active sources, a clipper never gets the same source twice until
they have had every active source once (one cycle); then a new cycle starts.
Within those candidates the least recently assigned source (globally) wins.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import threading
import time
from pathlib import Path

STORE_NAME = "top-videos.json"
REPO_ROOT = Path(__file__).resolve().parent
LOCAL_FALLBACK = REPO_ROOT / "data-local"

FAILED_RETENTION_S = 7 * 24 * 3600
STALE_WORK_S = 6 * 3600
# Newest delivered siblings kept per source for comparison. Older ones were already
# compared with their own neighbours; past this many, comparisons only grow cost.
SIBLING_KEEP = 50
LINK_TTL_S = 7 * 24 * 3600


class StoreUnavailable(Exception):
    """The store cannot be trusted (corrupt or inconsistent). Message is French."""


class NoActiveVideo(Exception):
    pass


class AlreadyReserved(Exception):
    pass


def _on_railway() -> bool:
    return any(os.getenv(k) for k in ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID"))


def resolve_data_dir() -> tuple[Path, bool, str]:
    """(path, persistent, warning). Falls back to ./data-local when DATA_DIR is not writable."""
    raw = os.getenv("DATA_DIR", "").strip() or "/data"
    path = Path(raw)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{secrets.token_hex(4)}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        LOCAL_FALLBACK.mkdir(parents=True, exist_ok=True)
        return (
            LOCAL_FALLBACK,
            False,
            f"DATA_DIR {raw} not writable ({exc.__class__.__name__}); using {LOCAL_FALLBACK}, NOT persistent",
        )
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if _on_railway():
        try:
            inside = bool(volume) and path.resolve().is_relative_to(Path(volume).resolve())
        except OSError:
            inside = False
        if not inside:
            return path, False, f"no Railway volume mounted at DATA_DIR {raw}: files are lost on redeploy"
    return path, True, ""


def _atomic_write(path: Path, payload: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class VideoStore:
    def __init__(self, root: Path, *, persistent: bool = True, warning: str = "") -> None:
        self.root = Path(root)
        self.persistent = persistent
        self.warning = warning
        self._lock = threading.RLock()
        for name in ("sources", "signatures", "links", "work"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        with self._lock:
            # Only a brand-new data dir gets a fresh store. A missing file next to existing
            # sources means something went wrong: _load() refuses instead of starting over.
            if not self.path.exists() and not any(
                not p.name.startswith(".") for p in self.sources_dir.iterdir()
            ):
                self._save({"version": 1, "salt": secrets.token_hex(32), "videos": [], "assignments": []})

    # -- paths ---------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.root / STORE_NAME

    @property
    def sources_dir(self) -> Path:
        return self.root / "sources"

    @property
    def links_dir(self) -> Path:
        return self.root / "links"

    @property
    def work_dir(self) -> Path:
        return self.root / "work"

    def source_path(self, video: dict) -> Path:
        name = Path(str(video["source_file"])).name  # never trust a stored path
        return self.sources_dir / name

    def _signature_path(self, assignment_id: str) -> Path:
        return self.root / "signatures" / f"{Path(assignment_id).name}.json"

    # -- load/save -----------------------------------------------------------
    def _load(self) -> dict:
        if not self.path.exists():
            raise StoreUnavailable(
                f"{STORE_NAME} a disparu alors que le dossier de données existe. "
                "Restaure le fichier plutôt que d’en recréer un vide."
            )
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StoreUnavailable(f"{STORE_NAME} illisible ({exc.__class__.__name__}).") from None
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("videos"), list)
            or not isinstance(data.get("assignments"), list)
            or not isinstance(data.get("salt"), str)
            or len(data["salt"]) < 32
        ):
            raise StoreUnavailable(f"{STORE_NAME} a un format inattendu.")
        return data

    def _save(self, data: dict) -> None:
        _atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))

    # -- videos --------------------------------------------------------------
    def add_video(self, *, source_file: str, title: str, sha256: str, size_bytes: int,
                  duration: float, width: int, height: int, added_by: int, video_id: str,
                  now: float | None = None) -> dict:
        now = now or time.time()
        with self._lock:
            data = self._load()
            video = {
                "id": video_id,
                "title": title,
                "source_file": Path(source_file).name,
                "sha256": sha256,
                "size_bytes": int(size_bytes),
                "duration": round(float(duration), 3),
                "width": int(width),
                "height": int(height),
                "added_by": int(added_by),
                "added_at": now,
                "active": True,
                "disabled_at": None,
                "disabled_by": None,
                "last_assigned_at": None,
                "assigned_count": 0,
                "last_pitch_sign": 0,
            }
            data["videos"].append(video)
            self._save(data)
            return dict(video)

    def find_active_by_sha(self, sha256: str) -> dict | None:
        with self._lock:
            for video in self._load()["videos"]:
                if video.get("active") and video.get("sha256") == sha256:
                    return dict(video)
        return None

    def list_videos(self) -> list[dict]:
        with self._lock:
            return [dict(v) for v in self._load()["videos"]]

    def get_video(self, video_id: str) -> dict | None:
        with self._lock:
            return next((dict(v) for v in self._load()["videos"] if v["id"] == video_id), None)

    def disable_video(self, video_id: str, by: int, now: float | None = None) -> dict | None:
        with self._lock:
            data = self._load()
            for video in data["videos"]:
                if video["id"] == video_id:
                    if video.get("active"):
                        video.update(active=False, disabled_at=now or time.time(), disabled_by=int(by))
                        self._save(data)
                    return dict(video)
        return None

    # -- assignments ---------------------------------------------------------
    def seed_for(self, salt: str, clipper_id: int, video_id: str, cycle: int) -> int:
        digest = hmac.new(bytes.fromhex(salt), f"{clipper_id}:{video_id}:{cycle}".encode(), hashlib.sha256).digest()
        return int.from_bytes(digest[:8], "big")

    @staticmethod
    def _live(assignments: list[dict]) -> list[dict]:
        return [a for a in assignments if a.get("status") in ("reserved", "delivered")]

    def reserve(self, clipper_id: int, *, thread_id: int | None = None, now: float | None = None) -> dict:
        now = now or time.time()
        with self._lock:
            data = self._load()
            live = self._live(data["assignments"])
            if any(a["clipper_id"] == clipper_id and a["status"] == "reserved" for a in live):
                raise AlreadyReserved()
            active = [v for v in data["videos"] if v.get("active") and self.source_path(v).is_file()]
            if not active:
                raise NoActiveVideo()
            mine = [a for a in live if a["clipper_id"] == clipper_id]
            cycle = max((a["cycle"] for a in mine), default=0)
            used = {a["video_id"] for a in mine if a["cycle"] == cycle}
            candidates = [v for v in active if v["id"] not in used]
            if not candidates:
                cycle += 1
                candidates = active
            video = min(candidates, key=lambda v: (v.get("last_assigned_at") or 0, v["added_at"], v["id"]))
            seed = self.seed_for(data["salt"], clipper_id, video["id"], cycle)
            siblings = sorted(
                (a for a in live if a["video_id"] == video["id"]), key=lambda a: a["created_at"]
            )
            previous = siblings[-1]["pitch_sign"] if siblings else video.get("last_pitch_sign", 0)
            pitch_sign = -previous if previous in (-1, 1) else (1 if seed & 1 else -1)
            assignment = {
                "id": secrets.token_hex(8),
                "video_id": video["id"],
                "clipper_id": int(clipper_id),
                "thread_id": int(thread_id) if thread_id else None,
                "cycle": cycle,
                "seed": seed,
                "pitch_sign": pitch_sign,
                "status": "reserved",
                "reason": "",
                "created_at": now,
                "updated_at": now,
                "delivered_at": None,
                "expires_at": None,
                "prev_last_assigned_at": video.get("last_assigned_at"),
                "has_signature": False,
            }
            video["last_assigned_at"] = now
            data["assignments"].append(assignment)
            self._save(data)
            return dict(assignment)

    def get_assignment(self, assignment_id: str) -> dict | None:
        with self._lock:
            return next((dict(a) for a in self._load()["assignments"] if a["id"] == assignment_id), None)

    def release(self, assignment_id: str, reason: str, now: float | None = None) -> bool:
        """A reserved assignment that will not be delivered stops counting for rotation."""
        with self._lock:
            data = self._load()
            for a in data["assignments"]:
                if a["id"] == assignment_id and a["status"] == "reserved":
                    a.update(status="failed", reason=reason[:200], updated_at=now or time.time())
                    for v in data["videos"]:
                        if v["id"] == a["video_id"] and v.get("last_assigned_at") == a["created_at"]:
                            v["last_assigned_at"] = a.get("prev_last_assigned_at")
                    self._save(data)
                    return True
        return False

    def mark_delivered(self, assignment_id: str, *, signature: dict, expires_at: float,
                       now: float | None = None) -> dict:
        now = now or time.time()
        with self._lock:
            data = self._load()
            a = next((x for x in data["assignments"] if x["id"] == assignment_id), None)
            if a is None or a["status"] != "reserved":
                raise StoreUnavailable("Assignation introuvable ou déjà clôturée.")
            _atomic_write(self._signature_path(assignment_id), json.dumps(signature).encode("utf-8"))
            a.update(status="delivered", delivered_at=now, updated_at=now,
                     expires_at=expires_at, has_signature=True)
            for v in data["videos"]:
                if v["id"] == a["video_id"]:
                    v["assigned_count"] = int(v.get("assigned_count", 0)) + 1
                    v["last_pitch_sign"] = a["pitch_sign"]
            self._save(data)
            return dict(a)

    def sibling_signatures(self, video_id: str) -> list[dict]:
        """Retained signatures of delivered siblings, newest first. Unreadable ones raise."""
        with self._lock:
            delivered = [
                a for a in self._load()["assignments"]
                if a["video_id"] == video_id and a["status"] == "delivered" and a.get("has_signature")
            ]
        delivered.sort(key=lambda a: a["delivered_at"] or 0, reverse=True)
        out = []
        for a in delivered[:SIBLING_KEEP]:
            try:
                out.append(json.loads(self._signature_path(a["id"]).read_text(encoding="utf-8")))
            except (OSError, ValueError):
                # A missing sibling would silently weaken the guard: refuse instead.
                raise StoreUnavailable(f"Signature de comparaison illisible ({a['id']}).") from None
        return out

    # -- restart / housekeeping ----------------------------------------------
    def recover_after_restart(self, now: float | None = None) -> list[dict]:
        """In-memory queue is gone after a restart: free every reservation it held."""
        with self._lock:
            data = self._load()
            stale = [a for a in data["assignments"] if a["status"] == "reserved"]
            for a in stale:
                a.update(status="failed", reason="restart", updated_at=now or time.time())
            if stale:
                self._save(data)
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return [dict(a) for a in stale]

    def housekeeping(self, now: float | None = None) -> dict:
        now = now or time.time()
        stats = {"failed_pruned": 0, "signatures_pruned": 0, "old_cycles_pruned": 0,
                 "sources_deleted": 0, "work_dirs_deleted": 0}
        with self._lock:
            data = self._load()
            keep: list[dict] = []
            for a in data["assignments"]:
                if a["status"] == "failed" and now - a["updated_at"] > FAILED_RETENTION_S:
                    stats["failed_pruned"] += 1
                    continue
                keep.append(a)
            # Signatures beyond the newest SIBLING_KEEP per source are never compared again.
            by_video: dict[str, list[dict]] = {}
            for a in keep:
                if a["status"] == "delivered" and a.get("has_signature"):
                    by_video.setdefault(a["video_id"], []).append(a)
            for rows in by_video.values():
                rows.sort(key=lambda a: a["delivered_at"] or 0, reverse=True)
                for a in rows[SIBLING_KEEP:]:
                    self._signature_path(a["id"]).unlink(missing_ok=True)
                    a["has_signature"] = False
                    stats["signatures_pruned"] += 1
            # Deliveries from a clipper's finished cycles only matter while their link lives.
            current = {}
            for a in self._live(keep):
                current[a["clipper_id"]] = max(current.get(a["clipper_id"], 0), a["cycle"])
            pruned: list[dict] = []
            for a in keep:
                old_cycle = a["status"] == "delivered" and a["cycle"] < current.get(a["clipper_id"], 0)
                expired = (a.get("expires_at") or 0) + 24 * 3600 < now
                if old_cycle and expired and not a.get("has_signature"):
                    stats["old_cycles_pruned"] += 1
                    continue
                pruned.append(a)
            data["assignments"] = pruned
            # Disabled sources with nothing in flight: delete the media, keep the record.
            in_flight = {a["video_id"] for a in pruned if a["status"] == "reserved"}
            for v in data["videos"]:
                if not v.get("active") and v["id"] not in in_flight and not v.get("source_deleted"):
                    self.source_path(v).unlink(missing_ok=True)
                    v["source_deleted"] = True
                    stats["sources_deleted"] += 1
            self._save(data)
        for child in self.work_dir.iterdir():
            try:
                if now - child.stat().st_mtime > STALE_WORK_S:
                    shutil.rmtree(child, ignore_errors=True)
                    stats["work_dirs_deleted"] += 1
            except OSError:
                pass
        for child in self.sources_dir.glob(".incoming-*"):
            try:
                if now - child.stat().st_mtime > 3600:
                    child.unlink(missing_ok=True)
            except OSError:
                pass
        return stats


_store: VideoStore | None = None
_store_lock = threading.Lock()


def get_store() -> VideoStore:
    global _store
    with _store_lock:
        if _store is None:
            root, persistent, warning = resolve_data_dir()
            if warning:
                print(f"[VIDEO] WARNING: {warning}")
            _store = VideoStore(root, persistent=persistent, warning=warning)
        return _store


def reset_store_for_tests(store: VideoStore | None = None) -> None:
    global _store
    with _store_lock:
        _store = store
