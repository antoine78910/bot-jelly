"""Shared Whop REST API helpers for one-off scripts."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.whop.com/api/v1"

SUPPORT_MESSAGE_SCOPES = [
    "chat:message:create",
    "chat:read",
    "support_chat:read",
    "support_chat:create",
    "support_chat:message:create",
]


class WhopClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key.strip()
        self._scoped_tokens: dict[str, str] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
        bearer: str | None = None,
    ) -> dict:
        url = API_BASE + path
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{url}?{query}"

        data = None
        headers = {
            "Authorization": f"Bearer {bearer or self.api_key}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Whop API {method} {path} failed ({exc.code}): {detail}") from exc

    def list_members(self, company_id: str) -> list[dict]:
        members: list[dict] = []
        after: str | None = None

        while True:
            payload = self.request(
                "GET",
                "/members",
                params={
                    "company_id": company_id,
                    "first": 100,
                    "after": after,
                },
            )
            members.extend(payload.get("data") or [])

            page_info = payload.get("page_info") or {}
            if not page_info.get("has_next_page"):
                break
            after = page_info.get("end_cursor")
            if not after:
                break

        return members

    def create_support_channel(self, company_id: str, user_id: str) -> dict:
        return self.request(
            "POST",
            "/support_channels",
            body={"company_id": company_id, "user_id": user_id},
        )

    def create_access_token(self, company_id: str, actor_user_id: str) -> str:
        cached = self._scoped_tokens.get(actor_user_id)
        if cached:
            return cached

        payload = self.request(
            "POST",
            "/access_tokens",
            body={
                "company_id": company_id,
                "user_id": actor_user_id,
                "scoped_actions": SUPPORT_MESSAGE_SCOPES,
            },
        )
        token = payload.get("token")
        if not token:
            raise RuntimeError(f"No access token returned: {payload}")
        self._scoped_tokens[actor_user_id] = str(token)
        return str(token)

    def send_message(self, channel_id: str, content: str, *, bearer: str | None = None) -> dict:
        return self.request(
            "POST",
            "/messages",
            body={"channel_id": channel_id, "content": content},
            bearer=bearer,
        )

    def send_support_message(
        self,
        company_id: str,
        actor_user_id: str,
        customer_user_id: str,
        content: str,
    ) -> dict:
        channel = self.create_support_channel(company_id, customer_user_id)
        channel_id = channel.get("id")
        if not channel_id:
            raise RuntimeError(f"No channel id returned: {channel}")

        token = self.create_access_token(company_id, actor_user_id)
        return self.send_message(channel_id, content, bearer=token)


def member_user_id(member: dict) -> str | None:
    user = member.get("user") or {}
    user_id = user.get("id")
    return str(user_id) if user_id else None


def member_email(member: dict) -> str | None:
    user = member.get("user") or {}
    email = user.get("email")
    if email and "@" in str(email):
        return str(email).strip().lower()
    return None


def is_customer_member(member: dict) -> bool:
    if member.get("status") != "joined":
        return False
    if member.get("access_level") == "admin":
        return False
    return member_user_id(member) is not None


def find_actor_user_id(members: list[dict], override: str = "") -> str:
    if override.strip():
        return override.strip()
    for member in members:
        if member.get("access_level") == "admin":
            user_id = member_user_id(member)
            if user_id:
                return user_id
    raise RuntimeError(
        "No admin user found for scoped access token. Set WHOP_ACTOR_USER_ID in .env."
    )
