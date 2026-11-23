"""Private download links for rendered videos: /v/<token> on the bot's own web server.

Each link is a folder links/<token>/ holding video.mp4 and manifest.json. A folder
is built under a .tmp- name and renamed into place, so a token either exists whole
or not at all, and it survives restarts. Unknown token: 404. Expired: 410, and the
folder is deleted. There is no listing route. Tokens never go to logs.
"""

from __future__ import annotations

import html
import json
import re
import secrets
import shutil
import time
from pathlib import Path

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from carousel_hosting import public_base_url
from video_store import LINK_TTL_S, get_store

LINK_TTL_LABEL = "7 jours"
VIDEO_FILE = "video.mp4"
_MANIFEST = "manifest.json"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_routes_registered = False


def links_root() -> Path:
    return get_store().links_dir


def hosting_ready() -> bool:
    """A link is only useful if the web server serves /v and the public URL is known."""
    return _routes_registered and public_base_url() is not None


def publish_video(path: Path, *, ttl: float = LINK_TTL_S, now: float | None = None) -> tuple[str, str, float]:
    """Copy a render into the link store. Returns (token, url, expires_at)."""
    base = public_base_url()
    if base is None:
        raise RuntimeError("PUBLIC_BASE_URL / RAILWAY_PUBLIC_DOMAIN is not set")
    root = links_root()
    now = now or time.time()
    token = secrets.token_urlsafe(32)
    tmp = root / f".tmp-{secrets.token_hex(8)}"
    tmp.mkdir(parents=True)
    try:
        shutil.copyfile(path, tmp / VIDEO_FILE)
        manifest = {
            "created_at": now,
            "expires_at": now + ttl,
            "filename": f"IMG_{secrets.randbelow(9000) + 1000}.mp4",
            "size": (tmp / VIDEO_FILE).stat().st_size,
        }
        (tmp / _MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
        tmp.rename(root / token)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return token, f"{base}/v/{token}", manifest["expires_at"]


def unpublish(token: str) -> None:
    if _TOKEN_RE.match(token):
        shutil.rmtree(links_root() / token, ignore_errors=True)


def cleanup_expired(now: float | None = None) -> int:
    now = now or time.time()
    removed = 0
    root = links_root()
    for folder in root.iterdir():
        if folder.name.startswith(".tmp-"):
            try:
                stale = now - folder.stat().st_mtime > 3600
            except OSError:
                stale = False
            if stale:
                shutil.rmtree(folder, ignore_errors=True)
            continue
        try:
            expires = json.loads((folder / _MANIFEST).read_text(encoding="utf-8")).get("expires_at", 0)
        except (OSError, ValueError):
            expires = 0
        if expires < now:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed


def _load(token: str) -> tuple[int, Path | None, dict]:
    """(status, folder, manifest): 200 ok, 404 unknown, 410 expired."""
    if not _TOKEN_RE.match(token):
        return 404, None, {}
    root = links_root()
    folder = root / token
    if folder.parent != root or not folder.is_dir():
        return 404, None, {}
    try:
        manifest = json.loads((folder / _MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 404, None, {}
    if float(manifest.get("expires_at", 0)) < time.time():
        shutil.rmtree(folder, ignore_errors=True)
        return 410, None, {}
    return 200, folder, manifest


_HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"}


def _message_page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='fr'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width'><meta name='robots' content='noindex'>"
        "<body style='background:#0b0b0f;color:#eee;font-family:system-ui;padding:32px'>"
        f"<h2>{html.escape(title)}</h2><p>{html.escape(body)}</p></body></html>"
    )


def _error(status: int) -> web.Response:
    if status == 410:
        page = _message_page("Lien expiré", "Reclique sur Vidéo dans Discord pour obtenir une nouvelle vidéo.")
    else:
        page = _message_page("Introuvable", "Ce lien n’existe pas.")
    return web.Response(status=status, text=page, content_type="text/html", headers=_HEADERS)


async def _page(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    status, _folder, manifest = _load(token)
    if status != 200:
        return _error(status)
    size_mb = manifest.get("size", 0) / 1e6
    src = f"/v/{token}/{VIDEO_FILE}"
    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Ta vidéo</title>
<style>body{{margin:0;background:#0b0b0f;color:#f2f2f2;font-family:system-ui,-apple-system,sans-serif}}
main{{max-width:480px;margin:0 auto;padding:20px 16px 40px}}
video{{width:100%;aspect-ratio:9/16;border-radius:12px;background:#1a1a22}}
.btn{{display:block;text-align:center;background:#5865f2;color:#fff;text-decoration:none;
padding:14px;border-radius:10px;margin-top:12px;font-weight:600}}
p{{color:#b8b8c0;font-size:14px;line-height:1.45}}</style></head><body><main>
<video src="{src}" controls playsinline preload="metadata"></video>
<a class="btn" href="{src}?dl=1" download>Télécharger la vidéo ({size_mb:.1f} Mo)</a>
<p>MP4 1080×1920, 60 images/s, qualité d’origine. Lien personnel valable {LINK_TTL_LABEL}, ne le partage pas.</p>
</main></body></html>"""
    return web.Response(text=page, content_type="text/html", headers=_HEADERS)


async def _file(request: web.Request) -> web.StreamResponse:
    status, folder, manifest = _load(request.match_info["token"])
    if status != 200:
        return _error(status)
    path = folder / VIDEO_FILE
    if not path.is_file():
        return _error(404)
    name = re.sub(r"[^A-Za-z0-9_.-]", "", str(manifest.get("filename") or "video.mp4")) or "video.mp4"
    disposition = "attachment" if request.query.get("dl") else "inline"
    return web.FileResponse(
        path,
        headers={
            **_HEADERS,
            "Content-Type": "video/mp4",
            "Content-Disposition": f'{disposition}; filename="{name}"',
            "Cache-Control": "private, no-store",
        },
    )


_TOKEN_PATH_RE = re.compile(r"^/(v|c)/[^/?]+")


def redact_path(path: str) -> str:
    return _TOKEN_PATH_RE.sub(lambda m: f"/{m.group(1)}/<token>", path)


class RedactingAccessLogger(AbstractAccessLogger):
    """aiohttp access log without download tokens (discord.py's logging setup prints INFO)."""

    def log(self, request, response, time) -> None:  # noqa: A002 (aiohttp signature)
        self.logger.info(
            "%s %s %s %s %.3fs",
            request.remote, request.method, redact_path(request.path), response.status, time,
        )


def register_routes(app: web.Application) -> None:
    global _routes_registered
    app.router.add_get("/v/{token}", _page)
    app.router.add_get(f"/v/{{token}}/{VIDEO_FILE}", _file)
    _routes_registered = True
