"""Test async core + role sync hook + OAuth helpers without Discord."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SESSION_SECRET", "test-secret")
DB_FILE = Path(__file__).resolve().parents[1] / "data" / "test_core.db"
for _suffix in ("", "-wal", "-shm"):
    Path(str(DB_FILE) + _suffix).unlink(missing_ok=True)
os.environ["DB_PATH"] = str(DB_FILE)

import auth  # noqa: E402
import core  # noqa: E402
import db  # noqa: E402

db.init_db()
db.seed_defaults()

failures: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + label + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        failures.append(label)


async def main() -> None:
    # clean slate for the test members
    for mid in (5005, 6006):
        row = db.get_member(mid)
        if row:
            with db.connect() as c:
                c.execute("DELETE FROM marks WHERE member_id=?", (mid,))
                c.execute("DELETE FROM members WHERE discord_id=?", (mid,))

    calls: list[tuple[int, int]] = []

    async def fake_syncer(discord_id: int, role_id: int) -> str:
        calls.append((discord_id, role_id))
        return ""

    core.set_role_syncer(fake_syncer)

    # wire a role onto the top rank so sync is triggered
    ranks = db.list_ranks()
    top = next(r for r in ranks if r["name"] == "Командир")
    veteran = next(r for r in ranks if r["name"] == "Ветеран")
    db.update_rank(int(top["id"]), role_id=999)
    db.update_rank(int(veteran["id"]), role_id=888)

    db.upsert_member(5005, username="x#1", display_name="Синх")
    actor = db.Actor(1, "Тест")

    res = await core.issue_mark(5005, "Поощрение", "молодец", 20, actor)
    check("mark applied score", res.score == 20, str(res.score))
    check("rank became Ветеран", res.new_rank is not None and res.new_rank["name"] == "Ветеран")
    check("role sync called with 888", calls == [(5005, 888)], str(calls))
    check("no role error", res.role_error == "", res.role_error)

    calls.clear()
    res = await core.issue_mark(5005, "Большое поощрение", "", 100, actor)
    check("promoted to Командир", res.new_rank["name"] == "Командир", str(res.new_rank["name"]))
    check("promoted flag", res.promoted is True)
    check("role sync called with 999", calls == [(5005, 999)], str(calls))

    # nothing changes rank -> no sync
    calls.clear()
    res = await core.issue_mark(5005, "Нейтрально", "", 0, actor)
    check("no sync when rank unchanged", calls == [], str(calls))

    # revoke drops score -> demote
    calls.clear()
    marks = db.list_marks(5005)
    big = next(m for m in marks if m["title"] == "Большое поощрение")
    ok, err = await core.revoke_mark(int(big["id"]), actor)
    check("revoke ok", ok and not err, err)
    check("demoted after revoke", db.get_member_full(5005)["rank_name"] == "Ветеран", db.get_member_full(5005)["rank_name"])
    check("sync on demote", calls == [(5005, 888)], str(calls))

    # give every rank a role id so the sync hook is exercised at any level
    for idx, r in enumerate(db.list_ranks()):
        db.update_rank(int(r["id"]), role_id=700 + idx)

    # role error surfaces
    async def failing_syncer(discord_id: int, role_id: int) -> str:
        return "нет прав на управление ролями"

    core.set_role_syncer(failing_syncer)
    db.upsert_member(6006, username="y#1", display_name="Ошибка")
    res = await core.issue_mark(6006, "Повышение", "", 90, actor)
    check("rank changed for 6006", res.rank_changed, str(res.rank_changed))
    check("role error surfaced", "нет прав" in res.role_error, res.role_error)

    # failing syncer raising still doesn't break the mark
    async def boom(discord_id: int, role_id: int) -> str:
        raise RuntimeError("сеть")

    core.set_role_syncer(boom)
    with db.connect() as c:
        c.execute("DELETE FROM marks WHERE member_id=6006")
        c.execute("UPDATE members SET score=0, rank_id=NULL WHERE discord_id=6006")
    res = await core.issue_mark(6006, "Ещё", "", 50, actor)
    check("mark survives syncer exception", res.score == 50, str(res.score))
    check("exception converted to error text", "ошибка" in res.role_error, res.role_error)

    # auto_rank disabled -> manual ranks only
    db.set_setting("auto_rank", "0")
    with db.connect() as c:
        c.execute("UPDATE members SET score=0, rank_id=NULL WHERE discord_id=6006")
        c.execute("DELETE FROM marks WHERE member_id=6006")
    res = await core.issue_mark(6006, "Без авторанга", "", 200, actor)
    check("no auto rank when disabled", res.rank_changed is False)
    check("rank stays empty", db.get_member(6006)["rank_id"] is None)
    db.set_setting("auto_rank", "1")

    # ---------------- auth helpers
    payload = auth.make_session_payload(
        {"id": "123456789", "username": "user", "global_name": "Ник", "avatar": None}
    )
    check("payload discord id", payload["discord_id"] == 123456789)
    check("payload display name", payload["display_name"] == "Ник")
    token = auth.dumps(payload)
    loaded = auth.loads(token)
    check("session roundtrip", loaded is not None and loaded["discord_id"] == 123456789)
    check("tampered session rejected", auth.loads(token + "x") is None)
    check("csrf match", auth.check_csrf(loaded, payload["csrf"]))
    check("csrf mismatch", not auth.check_csrf(loaded, "nope"))
    check("avatar fallback url", "embed/avatars" in payload["avatar"], payload["avatar"])

    url = auth.authorize_url("state123")
    check("authorize url host", url.startswith("https://discord.com/oauth2/authorize?"))
    check("authorize url has state", "state=state123" in url)
    check("authorize url has scope", "scope=identify" in url.replace("%20", " ") or "identify" in url)
    check("authorize url has redirect", "redirect_uri=" in url)

    print()
    print("FAILURES:", failures if failures else "none")


asyncio.run(main())
sys.exit(1 if failures else 0)
