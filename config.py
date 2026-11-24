"""Configuration loading for the faction management system."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")


def _csv_ids(raw: str | None) -> set[int]:
    out: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "").strip()
CLIENT_ID: str = os.getenv("CLIENT_ID", "").strip()
CLIENT_SECRET: str = os.getenv("CLIENT_SECRET", "").strip()
BASE_URL: str = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
PORT: int = int(os.getenv("PORT", "8000"))
HOST: str = os.getenv("HOST", "0.0.0.0")
DB_PATH: Path = Path(os.getenv("DB_PATH", "").strip() or str(DATA_DIR / "faction.db"))
if not DB_PATH.is_absolute():
    DB_PATH = (BASE_DIR / DB_PATH).resolve()
FACTION_NAME: str = os.getenv("FACTION_NAME", "Фракция")
COMMAND_GUILD_ID: int | None = (
    int(os.environ["COMMAND_GUILD_ID"]) if os.getenv("COMMAND_GUILD_ID", "").isdigit() else None
)
OFFICER_ROLE_IDS: set[int] = _csv_ids(os.getenv("OFFICER_ROLE_IDS"))
ADMIN_IDS: set[int] = _csv_ids(os.getenv("ADMIN_IDS"))


def _session_secret() -> str:
    """Use SESSION_SECRET from env, else a persisted random key."""
    env_secret = os.getenv("SESSION_SECRET", "").strip()
    if env_secret:
        return env_secret
    keyfile = DATA_DIR / "session.key"
    if keyfile.exists():
        return keyfile.read_text().strip()
    secret = secrets.token_urlsafe(48)
    keyfile.write_text(secret)
    keyfile.chmod(0o600)
    return secret


SESSION_SECRET: str = _session_secret()

OAUTH_REDIRECT_URI = f"{BASE_URL}/callback"
OAUTH_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
OAUTH_TOKEN_URL = "https://discord.com/api/oauth2/token"
OAUTH_SCOPES = "identify guilds.members.read"


def oauth_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)
