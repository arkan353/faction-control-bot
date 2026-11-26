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
        dept_id = member["department_id"] or db.default_department_id()
        target = db.resolve_rank_for_score(fresh_score, dept_id)
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
    if rank is not None and rank["department_id"]:
        # A rank belongs to a ladder; the member joins that department with it.
        db.update_member(member_id, department_id=int(rank["department_id"]))
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
        dept_id = member["department_id"] or db.default_department_id()
        target = db.resolve_rank_for_score(score, dept_id)
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
    dept_id = member["department_id"] or db.default_department_id()
    target = db.resolve_rank_for_score(int(member["score"]), dept_id)
    if target is None:
        return db.get_member_full(member_id)
    if int(member["rank_id"] or 0) != int(target["id"]):
        db.update_member(member_id, rank_id=int(target["id"]))
        await _sync_role(member_id, target)
    return db.get_member_full(member_id)


async def set_member_department(
    member_id: int, department_id: int | None, actor: db.Actor
) -> str:
    """Move a member into a department and give them a rank from its own ladder.

    The rank is never carried across ladders: rank names and thresholds differ per
    department, so the member enters at the rung their score earns there.
    """
    member = db.get_member_full(member_id)
    if member is None:
        raise ValueError("Участник не найден")

    target_rank: sqlite3.Row | None = None
    if department_id is None:
        db.update_member(member_id, department_id=None, rank_id=None)
    else:
        department = db.get_department(department_id)
        if department is None:
            raise ValueError("Отдел не найден")
        ladder = db.rank_ladder(department_id)
        if ladder:
            target_rank = db.resolve_rank_for_score(int(member["score"]), department_id)
        db.update_member(
            member_id,
            department_id=department_id,
            rank_id=int(target_rank["id"]) if target_rank else None,
        )

    db.log_action(
        actor.id,
        actor.name,
        "department.set",
        target=f"{member['display_name']} ({member_id})",
        details=(
            f"отдел → {department['name'] if department_id else 'без отдела'}"
            f" · ранг {target_rank['name'] if target_rank else '—'}"
        ),
    )
    return await _sync_role(member_id, target_rank)


def department_ladder(member_id: int) -> list[sqlite3.Row]:
    """Rungs a member can move through: their department ladder."""
    member = db.get_member(member_id)
    if member is None:
        return []
    dept_id = member["department_id"] or db.default_department_id()
    if dept_id is None:
        return []
    return db.rank_ladder(int(dept_id))


async def move_member(
    member_id: int, direction: int, steps: int = 1, actor: db.Actor | None = None
) -> tuple[sqlite3.Row | None, str]:
    """Step a member up (+1) or down (-1) their department ladder.

    Returns the resulting rank row and an error string ('' when the move happened).
    Movement is clamped to the ladder: the top and bottom rungs cannot be passed.
    A member who is not yet on their department ladder enters at its bottom rung.
    """
    member = db.get_member_full(member_id)
    if member is None:
        raise ValueError("Участник не найден")

    ladder = department_ladder(member_id)
    if not ladder:
        return None, "У отдела нет лестницы рангов"

    steps = max(1, abs(int(steps)))
    direction = 1 if direction > 0 else -1
    ids = [int(rank["id"]) for rank in ladder]
    current_id = int(member["rank_id"]) if member["rank_id"] else None

    entering = current_id not in ids
    if entering:
        # Entering the ladder starts at the bottom; demoting from nothing also
        # lands there, so an officer cannot drop a member below the ladder.
        index = -1
        new_index = 0
    else:
        index = ids.index(current_id)
        new_index = max(0, min(len(ids) - 1, index + direction * steps))
        if new_index == index:
            edge = "верхняя" if direction > 0 else "нижняя"
            return None, f"Это {edge} ступень отдела — дальше двигаться некуда"

    target = ladder[new_index]
    db.update_member(member_id, rank_id=int(target["id"]))
    if actor is not None:
        db.log_action(
            actor.id,
            actor.name,
            "rank.move",
            target=f"{member['display_name']} ({member_id})",
            details=(
                f"{member['rank_name'] or 'без ранга'} → {target['name']}"
                f" ({'вход в лестницу' if entering else ('повышение' if direction > 0 else 'понижение')}, {steps})"
            ),
        )
    err = await _sync_role(member_id, target)
    return target, err


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
