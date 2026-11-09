"""Personal Creator-platform invite for an approved Discord creator.

The Creator API creates the account in the Faceless category.

POST {CREATOR_BOT_API_BASE_URL}/api/v1/internal/discord-creator-invite
Header: x-creator-bot-invite-secret

A repeat call before the creator accepts revokes the pending invite and
issues a new link, valid for 30 days. Once they have accepted, the same
endpoint returns the dashboard login link (alreadyAccepted=true).

Missing configuration or an API failure must not block Discord approval.
Staff-facing errors never include the secret.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

INVITE_PATH = "/api/v1/internal/discord-creator-invite"
TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3  # first call + two retries
RETRY_BACKOFF_SECONDS = (0.5, 1.5)
RETRY_STATUSES = {429, 500, 502, 503, 504}
STATE_FILENAME = "creator-invite-backfill.json"
DRY_RUN_INVITE_URL = "<inviteUrl>"

_CODE_RE = re.compile(r"[A-Z0-9_]{1,80}")
_PLACEHOLDER_HANDLES = {
    "",
    "-",
    "—",
    "–",
    "n/a",
    "na",
    "none",
    "null",
    "pas encore",
    "pas encore (à configurer plus tard)",
    "pas encore (a configurer plus tard)",
}

_ERROR_LABELS = {
    "CONFIGURATION_MANQUANTE": (
        "configuration manquante (CREATOR_BOT_API_BASE_URL / CREATOR_BOT_INVITE_SECRET)"
    ),
    "TIMEOUT": "délai dépassé",
    "NETWORK": "erreur réseau",
    "INVITE_FAILED": "INVITE_FAILED",
}


@dataclass(frozen=True)
class InviteResult:
    invite_url: str
    creator_id: str
    already_accepted: bool


class CreatorInviteError(Exception):
    def __init__(self, status: int, code: str, *, retryable: bool = False) -> None:
        self.status = status
        self.code = code
        self.retryable = retryable
        super().__init__(self.public_message())

    def public_message(self) -> str:
        return redact(_public_error(self.status, self.code))


def redact(text: str) -> str:
    """Drop the invite secret if it ever leaks into a staff-facing string."""
    secret = os.getenv("CREATOR_BOT_INVITE_SECRET", "").strip()
    if secret and len(secret) >= 8 and secret in text:
        return text.replace(secret, "[redacted]")
    return text


def _public_error(status: int, code: str) -> str:
    label = _ERROR_LABELS.get(code, code or "INVITE_FAILED")
    if status:
        return f"{label} (HTTP {status})"
    return label


def invite_configured() -> bool:
    return bool(_api_base() and _api_secret())


def _api_base() -> str:
    return os.getenv("CREATOR_BOT_API_BASE_URL", "").strip().rstrip("/")


def _api_secret() -> str:
    return os.getenv("CREATOR_BOT_INVITE_SECRET", "").strip()


def social_handle(value: str | None) -> str | None:
    """Form handle, or None when the creator has no account yet."""
    text = str(value or "").strip().lstrip("@").strip()
    lowered = text.lower()
    if lowered in _PLACEHOLDER_HANDLES or lowered.startswith("pas encore"):
        return None
    return text[:100] or None


def invite_payload(record: dict, *, display_name: str | None = None) -> dict:
    """Body for the invite endpoint. Payment fields are never included."""
    discord_id = str(record.get("discord_id") or "").strip()
    username = str(record.get("discord_username") or "").strip().lstrip("@") or discord_id
    display = str(display_name or "").strip() or username
    body: dict[str, str] = {
        "discordId": discord_id,
        "discordUsername": username[:100],
        "displayName": display[:100],
    }
    instagram = social_handle(record.get("instagram"))
    tiktok = social_handle(record.get("tiktok"))
    if instagram:
        body["instagram"] = instagram
    if tiktok:
        body["tiktok"] = tiktok
    email = str(record.get("email") or "").strip()
    if email and "@" in email and " " not in email and not email.startswith("@"):
        body["email"] = email[:200]
    return body


def invite_paragraph(invite_url: str, *, already_accepted: bool = False) -> str:
    if already_accepted:
        detail = "lien de connexion à ton espace"
    else:
        detail = "lien personnel, valable 30 jours"
    return (
        "Suis tes vues et tes paiements sur ton espace Creator : "
        f"{invite_url} ({detail})."
    )


def backfill_dm(invite_url: str, *, already_accepted: bool = False) -> str:
    """DM for creators who were approved before this link existed."""
    return (
        "Ton inscription créateur JobShift est validée.\n\n"
        + invite_paragraph(invite_url, already_accepted=already_accepted)
    )


def invite_failure_message(record: dict, error: str) -> str:
    username = str(record.get("discord_username") or "").strip().lstrip("@")
    who = f" @{username}" if username else ""
    discord_id = record.get("discord_id") or "?"
    return redact(
        f"⚠️ Lien Creator non envoyé pour <@{discord_id}> (`{discord_id}`){who}. "
        "L’approbation et le DM de validation sont partis quand même. "
        f"Erreur : {error}."
    )


def _error_code(body: Any, status: int) -> str:
    fallback = {
        400: "INVALID_INPUT",
        401: "UNAUTHORIZED",
        409: "DISCORD_IDENTITY_CONFLICT",
        422: "NO_WORKSPACE_ADMIN",
        429: "RATE_LIMITED",
        500: "INVITE_FAILED",
        503: "SERVICE_UNAVAILABLE",
    }.get(status, "INVITE_FAILED")
    if not isinstance(body, dict):
        return fallback
    for key in ("code", "error", "errorCode"):
        value = body.get(key)
        if isinstance(value, dict):
            value = value.get("code") or value.get("error")
        if isinstance(value, str) and _CODE_RE.fullmatch(value.strip()):
            return value.strip()
    return fallback


def _success_body(body: Any) -> dict | None:
    if not isinstance(body, dict):
        return None
    if "inviteUrl" in body:
        return body
    data = body.get("data")
    if isinstance(data, dict) and "inviteUrl" in data:
        return data
    return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _parse_success(body: Any) -> InviteResult | None:
    data = _success_body(body)
    if not data:
        return None
    url = str(data.get("inviteUrl") or "").strip()
    if not url.startswith("https://"):
        return None
    return InviteResult(
        invite_url=url,
        creator_id=str(data.get("creatorId") or "").strip(),
        already_accepted=_as_bool(data.get("alreadyAccepted")),
    )


async def _read_json(response: aiohttp.ClientResponse) -> Any:
    try:
        return await response.json(content_type=None)
    except TimeoutError:
        raise
    except (aiohttp.ContentTypeError, ValueError, json.JSONDecodeError):
        return {}


async def request_creator_invite(record: dict, *, display_name: str | None = None) -> InviteResult:
    """Call the invite API. Raises CreatorInviteError (never includes the secret)."""
    base = _api_base()
    secret = _api_secret()
    if not base or not secret:
        raise CreatorInviteError(0, "CONFIGURATION_MANQUANTE")

    url = f"{base}{INVITE_PATH}"
    payload = invite_payload(record, display_name=display_name)
    headers = {
        "x-creator-bot-invite-secret": secret,
        "accept": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
    last: CreatorInviteError | None = None

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with session.post(url, json=payload, headers=headers) as response:
                    body = await _read_json(response)
                    if response.status == 200:
                        parsed = _parse_success(body)
                        if parsed is not None:
                            return parsed
                        last = CreatorInviteError(200, "INVITE_FAILED", retryable=True)
                    else:
                        last = CreatorInviteError(
                            response.status,
                            _error_code(body, response.status),
                            retryable=response.status in RETRY_STATUSES,
                        )
            except TimeoutError:
                last = CreatorInviteError(0, "TIMEOUT", retryable=True)
            except (aiohttp.ClientError, OSError):
                last = CreatorInviteError(0, "NETWORK", retryable=True)

            if last is None or not last.retryable or attempt + 1 >= MAX_ATTEMPTS:
                break
            await asyncio.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])

    raise last or CreatorInviteError(0, "INVITE_FAILED", retryable=False)


# ---------------------------------------------------------------------------
# Backfill selection (approved only) and the local idempotency file
# ---------------------------------------------------------------------------


def state_path_for(store_path: Path) -> Path:
    override = os.getenv("CREATOR_INVITE_STATE_PATH", "").strip()
    if override:
        return Path(override)
    return store_path.parent / STATE_FILENAME


def load_state(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"sent": {}}
    except (OSError, ValueError):
        return {"sent": {}}
    sent = raw.get("sent") if isinstance(raw, dict) else None
    if not isinstance(sent, dict):
        sent = {}
    return {"sent": {str(key): value for key, value in sent.items()}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def already_sent(record: dict, state: dict | None) -> bool:
    if record.get("creator_invite_sent") is True:
        return True
    sent = (state or {}).get("sent") or {}
    return str(record.get("discord_id") or "") in sent


def plan_backfill(
    records: list[dict],
    state: dict | None,
    *,
    only: str | None = None,
    limit: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """Approved accounts still waiting for the link, then those already marked sent."""
    approved = [record for record in records if record.get("status") == "approved"]
    if only:
        approved = [record for record in approved if str(record.get("discord_id")) == str(only)]
    approved.sort(key=lambda record: (str(record.get("applied_at") or ""), str(record.get("discord_id") or "")))
    pending: list[dict] = []
    skipped: list[dict] = []
    for record in approved:
        if already_sent(record, state):
            skipped.append(record)
        else:
            pending.append(record)
    if limit is not None:
        pending = pending[:limit]
    return pending, skipped


def only_status_message(records: list[dict], only: str) -> str | None:
    """Why --only matched nothing that can be sent, or None when it is approved."""
    matches = [record for record in records if str(record.get("discord_id")) == str(only)]
    if not matches:
        return f"Aucune inscription pour {only}."
    status = matches[-1].get("status") or "inconnu"
    if status != "approved":
        return (
            f"{only} a le statut {status}. "
            "Seules les inscriptions approuvées reçoivent le lien Creator."
        )
    return None


def format_dry_run(
    *,
    source: str,
    targets: list[dict],
    skipped: list[dict],
) -> str:
    lines = [
        "Mode : dry-run. Aucun appel à l’API Creator, aucun DM.",
        f"Source : {source}.",
        f"Approuvées à traiter : {len(targets)}. Déjà marquées (ignorées) : {len(skipped)}.",
    ]
    if skipped:
        shown = ", ".join(str(record.get("discord_id")) for record in skipped[:20])
        if len(skipped) > 20:
            shown += f" et {len(skipped) - 20} autre(s)"
        lines.append(f"Déjà traités : {shown}")
    lines.append("")
    if not targets:
        lines.append("Rien à envoyer.")
    for record in targets:
        username = str(record.get("discord_username") or "").strip().lstrip("@")
        header = f"{record.get('discord_id')}  @{username}" if username else str(record.get("discord_id"))
        lines.append(header)
        lines.append("DM :")
        lines.append(backfill_dm(DRY_RUN_INVITE_URL))
        lines.append("")
    lines.append(
        "Si le compte a déjà accepté l’invitation, la parenthèse devient : lien de connexion à ton espace."
    )
    lines.append("Envoi réel : python scripts/backfill_creator_invites.py --send")
    return "\n".join(lines).rstrip() + "\n"
