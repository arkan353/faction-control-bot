"""Exercise slash-command bodies with fake interactions (no network)."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DB_PATH"] = str(Path(__file__).resolve().parents[1] / "data" / "test_cmds.db")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SESSION_SECRET", "test-secret")

import bot as bot_module  # noqa: E402
import db  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + label + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        failures.append(label)


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.deferred = False

    async def send_message(self, content=None, *, embed=None, ephemeral=False, **kw):
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})

    async def defer(self, *, ephemeral=False, **kw):
        self.deferred = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, *, embed=None, ephemeral=False, **kw):
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class FakeUser:
    def __init__(self, uid: int, name: str, roles: list[int] | None = None, admin=False):
        self.id = uid
        self.name = name
        self.display_name = name
        self.roles = [type("R", (), {"id": r})() for r in (roles or [])]
        self.display_avatar = type("A", (), {"url": "https://cdn.discordapp.com/embed/avatars/0.png"})()
        self.guild_permissions = type(
            "P", (), {"administrator": admin, "manage_guild": admin}
        )()
        self.bot = False
        self.mention = f"<@{uid}>"

    def __str__(self) -> str:
        return f"{self.name}#0"

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)


class FakeGuildObj:
    def __init__(self, gid=1, name="Guild"):
        self.id = gid
        self.name = name
        self.members = []


class FakeInteraction:
    def __init__(self, user: FakeUser, guild=None) -> None:
        self.user = user
        self.guild = guild
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.command = None
        self.client = None


def command(bot_obj, qualified: str):
    parts = qualified.split()
    node = None
    for c in bot_obj.tree.get_commands():
        if c.name == parts[0]:
            node = c
            break
    for part in parts[1:]:
        node = next(x for x in node.commands if x.name == part)
    return node


async def main() -> None:
    db.init_db()
    db.seed_defaults()
    with db.connect() as c:
        c.execute("DELETE FROM marks")
        c.execute("DELETE FROM members")
    db.set_setting("faction_name", "Ночной Дозор")
    db.set_setting("auto_rank", "1")

    b = bot_module.build_bot()
    b.guild_id = 1
    b._guild = lambda: None  # role sync becomes a no-op in tests

    officer = FakeUser(1001, "Офицер", roles=[], admin=True)
    plain = FakeUser(2002, "Боец")
    guild = FakeGuildObj()

    # ---- /panel
    i = FakeInteraction(plain, guild)
    await command(b, "panel").callback(i)
    check("/panel responds", i.response.sent and i.response.sent[0]["ephemeral"])
    check("/panel created member", db.get_member(2002) is not None)
    embed = i.response.sent[0]["embed"]
    check("/panel has login link", "http://localhost:8000/login" in (embed.description or ""))

    # ---- /rank create (officer)
    i = FakeInteraction(officer, guild)
    await command(b, "rank create").callback(i, name="Лейтенант", role=None, level=6, min_score=200, color="#00ff88")
    check("/rank create confirms", "Лейтенант" in (i.response.sent[0]["content"] or ""))
    created = next((r for r in db.list_ranks() if r["name"] == "Лейтенант"), None)
    check("rank created in db", created is not None and created["level"] == 6)

    # ---- /rank create duplicate -> friendly error
    i = FakeInteraction(officer, guild)
    await command(b, "rank create").callback(i, name="Лейтенант", role=None, level=6, min_score=200, color="#00ff88")
    check("duplicate rank reported", "Не удалось" in (i.response.sent[0]["content"] or ""))

    # ---- /rank list
    i = FakeInteraction(plain, guild)
    await command(b, "rank list").callback(i)
    embed = i.response.sent[0]["embed"]
    check("/rank list has fields", len(embed.fields) >= 5, str(len(embed.fields)))

    # ---- /rank edit
    i = FakeInteraction(officer, guild)
    await command(b, "rank edit").callback(i, rank="Лейтенант", name=None, role=None, level=6, min_score=250, color=None)
    check("rank edit applied", db.get_rank(created["id"])["min_score"] == 250)
    check("rank edit confirms", "обновлён" in (i.response.sent[0]["content"] or ""))

    # ---- /rank edit missing rank
    i = FakeInteraction(officer, guild)
    await command(b, "rank edit").callback(i, rank="нет", name="x", role=None, level=None, min_score=None, color=None)
    check("rank edit missing reported", "не найден" in (i.response.sent[0]["content"] or ""))

    # ---- /mark give (no rank role bound -> no sync, but embed renders)
    i = FakeInteraction(officer, guild)
    await command(b, "mark give").callback(i, member=plain, title="Поощрение", delta=20, comment="За заслуги")
    check("/mark give deferred", i.response.deferred)
    embed = i.followup.sent[0]["embed"]
    check("mark embed title", "Поощрение" in (embed.title or ""))
    check("mark recorded", db.mark_stats(2002)["score"] == 20)
    check("rank auto-applied", db.get_member_full(2002)["rank_name"] is not None, str(db.get_member_full(2002)["rank_name"]))

    # ---- /mark give with negative delta
    i = FakeInteraction(officer, guild)
    await command(b, "mark give").callback(i, member=plain, title="Выговор", delta=-15, comment="")
    check("negative mark applied", db.get_member(2002)["score"] == 5)

    # ---- /mark revoke
    marks = db.list_marks(2002)
    target_mark = next(m for m in marks if m["title"] == "Выговор")
    i = FakeInteraction(officer, guild)
    await command(b, "mark revoke").callback(i, mark_id=int(target_mark["id"]))
    check("mark revoke confirms", "отозвана" in (i.followup.sent[0]["content"] or ""))
    check("score restored", db.get_member(2002)["score"] == 20)

    # ---- /mark revoke again -> error
    i = FakeInteraction(officer, guild)
    await command(b, "mark revoke").callback(i, mark_id=int(target_mark["id"]))
    check("double revoke reported", "уже отозвана" in (i.followup.sent[0]["content"] or ""))

    # ---- /file (member passed explicitly; interaction.user is a Member in a guild)
    i = FakeInteraction(plain, guild)
    await command(b, "file").callback(i, member=plain)
    embed = i.response.sent[0]["embed"]
    check("/file self renders", "Боец" in (embed.title or ""))
    check("/file shows marks", any("Поощрение" in (f.value or "") for f in embed.fields))
    check("/file shows rank", "Ранг" in (embed.description or ""))

    # ---- /file in DM (interaction.user is not a guild Member) -> friendly message
    i = FakeInteraction(plain, None)
    await command(b, "file").callback(i, member=None)
    check("/file without member warns", "Укажите участника" in (i.response.sent[0]["content"] or ""))

    # ---- /roster
    i = FakeInteraction(plain, guild)
    await command(b, "roster").callback(i)
    check("/roster renders", i.response.sent and i.response.sent[0]["embed"] is not None)

    # ---- /roster empty
    with db.connect() as c:
        c.execute("DELETE FROM members")
        c.execute("DELETE FROM marks")
    i = FakeInteraction(plain, guild)
    await command(b, "roster").callback(i)
    check("/roster empty message", "пуст" in (i.response.sent[0]["content"] or "").lower())

    # ---- /rank assign on a fresh member
    db.upsert_member(3003, username="x#1", display_name="Новый")
    veteran = next(r for r in db.list_ranks() if r["name"] == "Ветеран")
    i = FakeInteraction(officer, guild)
    await command(b, "rank assign").callback(i, member=FakeUser(3003, "Новый"), rank="Ветеран")
    check("rank assign deferred", i.response.deferred)
    check("rank assigned", db.get_member(3003)["rank_id"] == veteran["id"])
    check("assign confirms", "Ветеран" in (i.followup.sent[0]["content"] or ""))

    # ---- /sync and /setup
    i = FakeInteraction(officer, guild)
    await command(b, "sync").callback(i)
    check("/sync responds", i.followup.sent and "Состав обновлён" in (i.followup.sent[0]["content"] or ""))

    i = FakeInteraction(officer, guild)
    await command(b, "setup").callback(i)
    check("/setup stores guild", db.get_setting("guild_id") == "1")
    check("/setup renders embed", i.response.sent and i.response.sent[0]["embed"] is not None)

    # ---- officer permission gate
    check("officer passes gate", bot_module.is_officer(FakeInteraction(officer, guild)))
    check("plain fails gate", not bot_module.is_officer(FakeInteraction(plain, guild)))

    # ---- /rank delete
    i = FakeInteraction(officer, guild)
    await command(b, "rank delete").callback(i, rank="Лейтенант")
    check("rank deleted", db.get_rank(created["id"]) is None)
    check("delete confirms", "удалён" in (i.response.sent[0]["content"] or ""))

    print()
    print("FAILURES:", failures if failures else "none")


asyncio.run(main())
sys.exit(1 if failures else 0)
