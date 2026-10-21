"""Serve finished carousels from the bot's own web server, outside Discord.

Discord recompresses image attachments, so posters download the original PNGs
from a private link instead: /c/<token> (gallery), /c/<token>/<file>, /c/<token>/carrousel.zip.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import shutil
import time
import zipfile
from pathlib import Path

from aiohttp import web

LINK_TTL_SECONDS = 7 * 24 * 3600
LINK_TTL_LABEL = "7 jours"
ZIP_NAME = "carrousel.zip"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_MANIFEST = "manifest.json"


def store_dir() -> Path:
    raw = os.getenv("CAROUSEL_STORE_DIR", "").strip()
    if raw:
        return Path(raw)
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume:
        return Path(volume) / "carousels"
    return Path(__file__).parent / "carousel" / "delivered"


def public_base_url() -> str | None:
    explicit = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if domain:
        return f"https://{domain}"
    return None


def hosting_available() -> bool:
    return public_base_url() is not None


def cleanup_expired(now: float | None = None) -> None:
    root = store_dir()
    if not root.is_dir():
        return
    now = now or time.time()
    for folder in root.iterdir():
        manifest = folder / _MANIFEST
        try:
            expires = json.loads(manifest.read_text(encoding="utf-8")).get("expires_at", 0)
        except (OSError, ValueError):
            expires = 0
        if expires < now:
            shutil.rmtree(folder, ignore_errors=True)


def publish_carousel(named_slides: list[tuple[Path, str]], *, title: str) -> str:
    """Copy original PNGs into the store and return the public gallery URL."""
    base = public_base_url()
    if base is None:
        raise RuntimeError("PUBLIC_BASE_URL / RAILWAY_PUBLIC_DOMAIN is not set")

    cleanup_expired()
    token = secrets.token_urlsafe(18)
    folder = store_dir() / token
    folder.mkdir(parents=True, exist_ok=True)

    files: list[str] = []
    with zipfile.ZipFile(folder / ZIP_NAME, "w", compression=zipfile.ZIP_STORED) as archive:
        for path, filename in named_slides:
            shutil.copyfile(path, folder / filename)
            archive.write(path, arcname=filename)
            files.append(filename)

    manifest = {
        "title": title,
        "files": files,
        "created_at": time.time(),
        "expires_at": time.time() + LINK_TTL_SECONDS,
    }
    (folder / _MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return f"{base}/c/{token}"


def _load(token: str) -> tuple[Path, dict] | None:
    if not _TOKEN_RE.match(token):
        return None
    folder = store_dir() / token
    try:
        manifest = json.loads((folder / _MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if manifest.get("expires_at", 0) < time.time():
        shutil.rmtree(folder, ignore_errors=True)
        return None
    return folder, manifest


_EXPIRED_PAGE = (
    "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
    "<body style='background:#0b0b0f;color:#eee;font-family:system-ui;padding:32px'>"
    "<h2>Lien expiré</h2><p>Relance une génération sur Discord pour obtenir un nouveau lien.</p>"
)


def _gallery_html(token: str, manifest: dict) -> str:
    title = html.escape(manifest.get("title") or "Carrousel")
    items = []
    for index, name in enumerate(manifest["files"], start=1):
        safe = html.escape(name)
        items.append(
            f"<figure><img src='/c/{token}/{safe}' alt='Slide {index}' loading='lazy'>"
            f"<a class='btn' href='/c/{token}/{safe}?dl=1' download='{safe}'>"
            f"Télécharger la slide {index}</a></figure>"
        )
    files_json = json.dumps(manifest["files"])
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{title} — JobShift</title>
<style>
body{{margin:0;background:#0b0b0f;color:#f2f2f2;font-family:system-ui,-apple-system,sans-serif}}
main{{max-width:560px;margin:0 auto;padding:20px 16px 48px}}
h1{{font-size:20px;margin:4px 0 6px}} p{{color:#b8b8c0;font-size:14px;line-height:1.45}}
.btn{{display:block;text-align:center;background:#2b2b35;color:#fff;text-decoration:none;
padding:12px;border-radius:10px;margin-top:8px;font-weight:600;border:0;width:100%;font-size:15px}}
.primary{{background:#5865f2}} figure{{margin:22px 0}}
img{{width:100%;height:auto;border-radius:12px;display:block}}
#share{{display:none}}
</style></head><body><main>
<h1>{title}</h1>
<p>Images en qualité d’origine (PNG, sans compression Discord).<br>
Sur iPhone : <b>Enregistrer toutes les photos</b>, ou appui long sur une image → <i>Ajouter à Photos</i>.</p>
<button id="share" class="btn primary">Enregistrer toutes les photos</button>
<a class="btn" href="/c/{token}/{ZIP_NAME}?dl=1" download>Tout télécharger (ZIP, ordinateur)</a>
{''.join(items)}
<p>Lien valable {LINK_TTL_LABEL}.</p>
</main>
<script>
const files = {files_json};
const btn = document.getElementById('share');
if (navigator.canShare) {{
  btn.style.display = 'block';
  btn.onclick = async () => {{
    btn.textContent = 'Préparation…';
    try {{
      const blobs = await Promise.all(files.map(f => fetch('/c/{token}/' + f).then(r => r.blob())));
      const list = blobs.map((b, i) => new File([b], files[i], {{type: 'image/png'}}));
      if (navigator.canShare({{files: list}})) {{ await navigator.share({{files: list}}); }}
    }} catch (e) {{}}
    btn.textContent = 'Enregistrer toutes les photos';
  }};
}}
</script></body></html>"""


async def _gallery(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    loaded = _load(token)
    if loaded is None:
        return web.Response(status=404, text=_EXPIRED_PAGE, content_type="text/html")
    _folder, manifest = loaded
    return web.Response(
        text=_gallery_html(token, manifest),
        content_type="text/html",
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )


async def _file(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    name = request.match_info["name"]
    loaded = _load(token)
    if loaded is None:
        return web.Response(status=404, text="Lien expiré")
    folder, manifest = loaded
    if name != ZIP_NAME and name not in manifest["files"]:
        return web.Response(status=404, text="Introuvable")
    path = folder / name
    if not path.is_file():
        return web.Response(status=404, text="Introuvable")
    disposition = "attachment" if request.query.get("dl") else "inline"
    return web.FileResponse(
        path,
        headers={
            "Content-Disposition": f'{disposition}; filename="{name}"',
            "Cache-Control": "private, max-age=86400",
            "X-Robots-Tag": "noindex",
        },
    )


def register_routes(app: web.Application) -> None:
    app.router.add_get("/c/{token}", _gallery)
    app.router.add_get("/c/{token}/{name}", _file)
