"""Jinja filters."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def dt(value: Any) -> str:
    """Render a unix timestamp as a readable local date-time."""
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")


def short_date(value: Any) -> str:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%d.%m.%y")
