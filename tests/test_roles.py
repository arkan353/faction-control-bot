"""Test Discord role synchronisation logic with stubbed guild objects."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
DB_FILE = Path(__file__).resolve().parents[1] / "data" / "test_role.db"
for _suffix in ("", "-wal", "-shm"):
    Path(str(DB_FILE) + _suffix).unlink(missing_ok=True)
os.environ["DB_PATH"] = str(DB_FILE)
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SESSION_SECRET", "test-secret")

import bot as bot_module  # noqa: E402
import db  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + label + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        failures.append(label)


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class FakeMember:
    def __init__(self, member_id: int, role_ids: list[int], *, fail: bool = False) -> None:
        self.id = member_id
        self.roles = [FakeRole(r) for r in role_ids]
        self.fail = fail
        self.added: list[int] = []
        self.removed: list[int] = []

    def get_role(self, role_id: int):
        return next((r for r in self.roles if r.id == role_id), None)

    def _with(self, role_id: int) -> "FakeMember":
        return FakeMember(self.id, [r.id for r in self.roles] + [role_id], fail=self.fail)

    async def add_roles(self, *roles, reason: str = "") -> None:
        if self.fail:
            import discord

            raise discord.Forbidden(_Resp(), "missing permissions")
        for r in roles:
            self.added.append(r.id)
            if self.get_role(r.id) is None:
                self.roles.append(r)

    async def remove_roles(self, *roles, reason: str = "") -> None:
        if self.fail:
            import discord

            raise discord.Forbidden(_Resp(), "missing permissions")
        for r in roles:
            self.removed.append(r.id)
            self.roles = [x for x in self.roles if x.id != r.id]


class _Resp:
    status = 403
    reason = "Forbidden"


class FakeGuild:
    def __init__(self, members: dict[int, FakeMember], roles: list[FakeRole]) -> None:
        self.id = 1
        self.members = list(members.values())
        self._members = members
        self._roles = {r.id: r for r in roles}
        self.name = "Test Guild"

    def get_member(self, member_id: int):
        return self._members.get(member_id)

    async def fetch_member(self, member_id: int):
        import discord

        if member_id not in self._members:
            raise discord.NotFound(_Resp(), "unknown member")
        return self._members[member_id]

    def get_role(self, role_id: int):
        return self._roles.get(role_id)


async def main() -> None:
    db.init_db()
    db.seed_defaults()
    with db.connect() as c:
        c.execute("DELETE FROM marks")
        c.execute("DELETE FROM members")

    ranks = {r["name"]: int(r["id"]) for r in db.list_ranks()}
    fighter = ranks["Боец"]
    veteran = ranks["Ветеран"]
    other = ranks["Командир"]
    db.update_rank(fighter, role_id=501)
    db.update_rank(veteran, role_id=502)
    db.update_rank(other, role_id=503)

    b = bot_module.build_bot()
    b.guild_id = 1

    # removes the old rank role and adds the new one
    member = FakeMember(9001, [501])
    b._guild = lambda: FakeGuild({9001: member}, [FakeRole(501), FakeRole(502), FakeRole(503)])  # type: ignore[method-assign]
    err = await b.sync_member_role(9001, 502)
    check("no error on sync", err == "", err)
    check("added new role 502", member.added == [502], str(member.added))
    check("removed old rank role 501", member.removed == [501], str(member.removed))

    # already has the target role -> no add, strips stale ranks
    member2 = FakeMember(9002, [502, 501])
    member2.roles = [FakeRole(502), FakeRole(501)]
    b._guild = lambda: FakeGuild({9002: member2}, [FakeRole(501), FakeRole(502)])  # type: ignore[method-assign]
    err = await b.sync_member_role(9002, 502)
    check("no error when role present", err == "", err)
    check("did not re-add", member2.added == [], str(member2.added))
    check("removed stale role", member2.removed == [501], str(member2.removed))

    # non-rank role is untouched
    member3 = FakeMember(9003, [111, 501])
    member3.roles = [FakeRole(111), FakeRole(501)]
    b._guild = lambda: FakeGuild({9003: member3}, [FakeRole(501), FakeRole(502)])  # type: ignore[method-assign]
    await b.sync_member_role(9003, 502)
    check("kept unrelated role", all(r.id != 111 for r in [FakeRole(x) for x in member3.removed]))

    # permission failure is reported, not raised
    member4 = FakeMember(9004, [501], fail=True)
    b._guild = lambda: FakeGuild({9004: member4}, [FakeRole(501), FakeRole(502)])  # type: ignore[method-assign]
    err = await b.sync_member_role(9004, 502)
    check("forbidden reported", "прав" in err, err)

    # missing role on the guild
    member5 = FakeMember(9005, [])
    b._guild = lambda: FakeGuild({9005: member5}, [FakeRole(501)])  # type: ignore[method-assign]
    err = await b.sync_member_role(9005, 999)
    check("missing role reported", "не найдена" in err, err)

    # member not in guild
    b._guild = lambda: FakeGuild({}, [FakeRole(502)])  # type: ignore[method-assign]
    err = await b.sync_member_role(9006, 502)
    check("absent member reported", "не найден" in err, err)

    # no guild configured (restore the real _guild lookup)
    del b._guild
    b.guild_id = None
    b._connection = None
    err = await b.sync_member_role(9001, 502)
    check("no guild reported", "гильдия" in err, err)
    b.guild_id = 1

    # _find_rank lookups
    check("find by exact name", bot_module._find_rank("Ветеран")["id"] == veteran)
    check("find by id", bot_module._find_rank(str(other))["id"] == other)
    check("find by partial", bot_module._find_rank("вет")["id"] == veteran)
    check("find missing returns None", bot_module._find_rank("нет такого") is None)
    check("fighter id", fighter > 0)

    print()
    print("FAILURES:", failures if failures else "none")


asyncio.run(main())
sys.exit(1 if failures else 0)
