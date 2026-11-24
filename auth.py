"""Discord OAuth2 login + signed-cookie sessions for the web panel."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer

from config import (
    CLIENT_ID,
    CLIENT_SECRET,
    OAUTH_AUTHORIZE_URL,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPES,
    OAUTH_TOKEN_URL,
    SESSION_SECRET,
)

SESSION_COOKIE = "faction_session"
STATE_COOKIE = "faction_oauth_state"
NEXT_COOKIE = "faction_oauth_next"
SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours
_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="faction-session")


@dataclass
class SessionUser:
    discord_id: int
    username: str
    display_name: str
    avatar: str
    csrf: str

    @property
    def initials(self) -> str:
        return (self.display_name or self.username or "?")[:2].upper()


def new_state() -> str:
    return secrets.token_urlsafe(24)


def authorize_url(state: str) -> str:
    params = httpx.QueryParams(
        client_id=CLIENT_ID,
        redirect_uri=OAUTH_REDIRECT_URI,
        response_type="code",
        scope=OAUTH_SCOPES,
        prompt="consent",
        state=state,
    )
    return f"{OAUTH_AUTHORIZE_URL}?{params}"


async def exchange_code(code: str) -> tuple[dict[str, Any], str]:
    """Trade the OAuth code for a token, then fetch the Discord profile.

    Returns (profile, access_token). The token is used once to read the
    member's roles on the faction guild and is never persisted.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": OAUTH_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_resp.raise_for_status()
        token = token_resp.json()
        access_token = token.get("access_token")
        if not access_token:
            raise ValueError("Discord не вернул access_token")
        me_resp = await client.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        me_resp.raise_for_status()
        return me_resp.json(), str(access_token)


async def fetch_guild_roles(access_token: str, guild_id: int) -> list[int]:
    """Use guilds.members.read to resolve the user's roles on the faction guild."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"https://discord.com/api/users/@me/guilds/{guild_id}/member",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        return []
    return [int(r) for r in resp.json().get("roles", [])]


def avatar_url(user: dict[str, Any]) -> str:
    uid = user.get("id")
    avatar = user.get("avatar")
    if avatar:
        ext = "gif" if str(avatar).startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.{ext}?size=128"
    idx = (int(uid) >> 22) % 6 if uid else 0
    return f"https://cdn.discordapp.com/embed/avatars/{idx}.png"


def make_session_payload(user: dict[str, Any]) -> dict[str, Any]:
    username = user.get("username") or "unknown"
    return {
        "discord_id": int(user["id"]),
        "username": username,
        "display_name": user.get("global_name") or username,
        "avatar": avatar_url(user),
        "csrf": secrets.token_urlsafe(24),
        "issued": int(time.time()),
    }


def dumps(payload: dict[str, Any]) -> str:
    return _serializer.dumps(payload)


def loads(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, Exception):  # expired or tampered
        return None
    return data if isinstance(data, dict) else None


def session_user(payload: dict[str, Any]) -> SessionUser:
    return SessionUser(
        discord_id=int(payload["discord_id"]),
        username=payload.get("username", ""),
        display_name=payload.get("display_name", ""),
        avatar=payload.get("avatar", ""),
        csrf=payload.get("csrf", ""),
    )


def check_csrf(payload: dict[str, Any] | None, token: str | None) -> bool:
    if not payload or not token:
        return False
    return secrets.compare_digest(str(payload.get("csrf", "")), str(token))
