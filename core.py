"""Shared business logic: marks, rank resolution, Discord role sync hooks."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import db
from config import FACTION_NAME

log = logging.getLogger("faction.core")

# Installed by the bot process: (discord_id, role_id) -> error string ('' = ok).
_role_syncer: Callable[[int, int], Awaitable[str]] | None = None


def set_role_syncer(fn: Callable[[int, int], Awaitable[str]] | None) -> None:
    global _role_syncer
    _role_syncer = fn


@dataclass
class MarkResult:
    member_id: int
    mark_id: int
    score: int
    old_rank: sqlite3.Row | None = None
    new_rank: sqlite3.Row | None = None
    rank_changed: bool = False
    promoted: bool = False
    demoted: bool = False
    role_error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def auto_rank_enabled() -> bool:
    return db.get_setting("auto_rank", "1") == "1"


async def _sync_role(member_id: int, rank: sqlite3.Row | None) -> str:
    """Push the rank role to Discord. '' means done or nothing to do."""
    if _role_syncer is None or rank is None or not rank["role_id"]:
        return ""
    try:
        return await _role_syncer(member_id, int(rank["role_id"]))
    except Exception as exc:  # noqa: BLE001 - never break a mark on a role error
        log.warning("Синхронизация роли не удалась для %s: %s", member_id, exc)
        return f"ошибка синхронизации роли: {exc}"


async def issue_mark(
    member_id: int,
    title: str,
    comment: str,
    delta: int,
    actor: db.Actor,
) -> MarkResult:
    """Record a mark and, when enabled, recompute and apply the member's rank."""
    member = db.get_member_full(member_id)
    if member is None:
        raise ValueError("Участник не найден")

    old_rank = db.get_rank(int(member["rank_id"])) if member["rank_id"] else None
    mark_id = db.add_mark(member_id, title, comment, delta, actor.id, actor.name)

    new_rank = old_rank
    changed = False
    if auto_rank_enabled():
        fresh_score = int(db.get_member(member_id)["score"])  # type: ignore[index]
        target = db.resolve_rank_for_score(fresh_score)
        if target is not None and (old_rank is None or int(target["id"]) != int(old_rank["id"])):
            db.update_member(member_id, rank_id=int(target["id"]))
            new_rank = target
            changed = True

    fresh = db.get_member_full(member_id)
    score = int(fresh["score"]) if fresh else 0
    promoted = bool(
        changed
        and new_rank is not None
        and (old_rank is None or int(new_rank["level"]) > int(old_rank["level"]))
    )
    demoted = bool(
        changed
        and old_rank is not None
        and new_rank is not None
        and int(new_rank["level"]) < int(old_rank["level"])
    )

    db.log_action(
        actor.id,
        actor.name,
        "mark.issue",
        target=f"{member['display_name']} ({member_id})",
        details=f"«{title}» {delta:+d} → ранг {new_rank['name'] if new_rank else '—'}",
    )

    role_error = await _sync_role(member_id, new_rank) if changed else ""
    return MarkResult(
        member_id=member_id,
        mark_id=mark_id,
        score=score,
        old_rank=old_rank,
        new_rank=new_rank,
        rank_changed=changed,
        promoted=promoted,
        demoted=demoted,
        role_error=role_error,
    )


async def set_member_rank(member_id: int, rank_id: int | None, actor: db.Actor) -> str:
    member = db.get_member_full(member_id)
    if member is None:
        raise ValueError("Участник не найден")
    rank = db.get_rank(rank_id) if rank_id else None
    db.update_member(member_id, rank_id=rank_id)
    db.log_action(
        actor.id,
        actor.name,
        "rank.set",
        target=f"{member['display_name']} ({member_id})",
        details=f"ранг → {rank['name'] if rank else 'без ранга'}",
    )
    return await _sync_role(member_id, rank)


async def revoke_mark(mark_id: int, actor: db.Actor) -> tuple[bool, str]:
    mark = db.get_mark(mark_id)
    if mark is None:
        return False, "Отметка не найдена"
    if not db.revoke_mark(mark_id, actor.name):
        return False, "Отметка уже отозвана"

    member_id = int(mark["member_id"])
    member = db.get_member_full(member_id)
    if member is not None and auto_rank_enabled():
        score = int(db.get_member(member_id)["score"])  # type: ignore[index]
        target = db.resolve_rank_for_score(score)
        if target is not None and int(member["rank_id"] or 0) != int(target["id"]):
            db.update_member(member_id, rank_id=int(target["id"]))
            await _sync_role(member_id, target)

    db.log_action(
        actor.id,
        actor.name,
        "mark.revoke",
        target=f"{member['display_name'] if member else member_id}",
        details=f"«{mark['title']}» {int(mark['delta']):+d}",
    )
    return True, ""


async def recalc_member_rank(member_id: int) -> sqlite3.Row | None:
    """Re-apply score-based rank for one member, syncing the role if it changed."""
    if not auto_rank_enabled():
        return db.get_member_full(member_id)
    member = db.get_member(member_id)
    if member is None:
        return None
    target = db.resolve_rank_for_score(int(member["score"]))
    if target is None:
        return db.get_member_full(member_id)
    if int(member["rank_id"] or 0) != int(target["id"]):
        db.update_member(member_id, rank_id=int(target["id"]))
        await _sync_role(member_id, target)
    return db.get_member_full(member_id)


def ladder_progress(score: int, ranks: list[Any]) -> dict[str, Any]:
    """Describe where the member sits on the ladder and what comes next."""
    ordered = sorted(ranks, key=lambda r: (int(r["level"]), int(r["min_score"])))
    current_idx = -1
    for i, rank in enumerate(ordered):
        if score >= int(rank["min_score"]):
            current_idx = i
    cur = ordered[current_idx] if current_idx >= 0 else None
    nxt = ordered[current_idx + 1] if 0 <= current_idx + 1 < len(ordered) else None
    if nxt is None:
        return {"current": cur, "next": None, "percent": 100, "remaining": 0}
    base = int(cur["min_score"]) if cur else 0
    span = max(1, int(nxt["min_score"]) - base)
    return {
        "current": cur,
        "next": nxt,
        "percent": int(max(0, min(100, (score - base) * 100 / span))),
        "remaining": max(0, int(nxt["min_score"]) - score),
    }


def faction_name() -> str:
    return db.get_setting("faction_name", FACTION_NAME) or FACTION_NAME
