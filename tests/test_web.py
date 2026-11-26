"""Offline test of the web panel using a forged session cookie."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
DB_FILE = Path(__file__).resolve().parents[1] / "data" / "test_web.db"
for _suffix in ("", "-wal", "-shm"):
    Path(str(DB_FILE) + _suffix).unlink(missing_ok=True)
os.environ["DB_PATH"] = str(DB_FILE)
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SESSION_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
import main  # noqa: E402

db.init_db()
db.seed_defaults()

client = TestClient(main.app)


def session_for(discord_id: int, officer: bool, name: str) -> None:
    db.upsert_member(discord_id, username=f"{name}#1", display_name=name, officer=officer)
    payload = {
        "discord_id": discord_id,
        "username": f"{name}#1",
        "display_name": name,
        "avatar": "https://cdn.discordapp.com/embed/avatars/0.png",
        "csrf": "testcsrf",
        "issued": 0,
    }
    client.cookies.set(auth.SESSION_COOKIE, auth.dumps(payload))


failures = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + label + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        failures.append(label)


# anonymous
r = client.get("/", follow_redirects=False)
check("anon / redirects to login", r.status_code == 303)
r = client.get("/members", follow_redirects=False)
check("anon /members redirects to login", r.status_code == 303)

# officer
session_for(1001, True, "Офицер Тест")
r = client.get("/")
check("officer / renders", r.status_code == 200, str(r.status_code))
check("index shows overview", "Обзор" in r.text)
r = client.get("/members")
check("officer /members 200", r.status_code == 200, str(r.status_code))
r = client.get("/ranks")
check("officer /ranks 200", r.status_code == 200, str(r.status_code))
check("ranks page lists seed rank", "Командир" in r.text)
r = client.get("/settings")
check("officer /settings 200", r.status_code == 200, str(r.status_code))

# create a rank
r = client.post(
    "/ranks",
    data={
        "csrf_token": "testcsrf",
        "name": "Тестовый ранг",
        "role_id": "",
        "level": "7",
        "min_score": "120",
        "color": "#123456",
        "description": "проверка",
    },
    follow_redirects=False,
)
check("create rank redirects", r.status_code == 303, str(r.status_code))
check("rank exists in db", any(x["name"] == "Тестовый ранг" for x in db.list_ranks()))

# create a member + mark
db.upsert_member(2002, username="boy#1", display_name="Боец Тест")
r = client.post(
    "/members/2002/marks",
    data={"csrf_token": "testcsrf", "title": "Нарушение", "delta": "-10", "comment": "тест"},
    follow_redirects=False,
)
check("mark post redirects", r.status_code == 303, str(r.status_code))
check("mark stored", db.mark_stats(2002)["negative"] == 1)
check("score negative", db.get_member(2002)["score"] == -10)

r = client.get("/members/2002")
check("member dossier 200", r.status_code == 200, str(r.status_code))
check("dossier shows mark", "Нарушение" in r.text)

# manual rank assignment
rid = next(x["id"] for x in db.list_ranks() if x["name"] == "Тестовый ранг")
r = client.post(
    "/members/2002/rank",
    data={"csrf_token": "testcsrf", "rank_id": str(rid)},
    follow_redirects=False,
)
check("rank assign redirects", r.status_code == 303, str(r.status_code))
check("rank assigned", db.get_member(2002)["rank_id"] == rid)

# revoke the mark
mark_id = db.list_marks(2002)[0]["id"]
r = client.post(f"/marks/{mark_id}/revoke", data={"csrf_token": "testcsrf"}, follow_redirects=False)
check("revoke redirects", r.status_code == 303, str(r.status_code))
check("mark revoked", db.mark_stats(2002)["negative"] == 0)

# csrf rejection
r = client.post(
    "/members/2002/marks",
    data={"csrf_token": "wrong", "title": "x", "delta": "1"},
    follow_redirects=False,
)
check("bad csrf rejected", r.status_code == 400, str(r.status_code))

# settings save
r = client.post(
    "/settings",
    data={"csrf_token": "testcsrf", "faction_name": "Ночной Дозор", "auto_rank": "on"},
    follow_redirects=False,
)
check("settings redirect", r.status_code == 303, str(r.status_code))
check("setting saved", db.get_setting("faction_name") == "Ночной Дозор")

# plain member: cannot open others
session_for(3003, False, "Рядовой")
r = client.get("/members", follow_redirects=False)
check("member /members forbidden", r.status_code == 403, str(r.status_code))
r = client.get("/members/2002", follow_redirects=False)
check("member other dossier forbidden", r.status_code == 403, str(r.status_code))
r = client.get("/members/3003")
check("member own dossier 200", r.status_code == 200, str(r.status_code))
r = client.get("/ranks", follow_redirects=False)
check("member /ranks forbidden", r.status_code == 403, str(r.status_code))

# health
r = client.get("/health")
check("health ok", r.status_code == 200 and r.json()["status"] == "ok")

# ---------------------------------------------------------------- departments
session_for(1001, True, "Офицер Тест")
r = client.get("/departments")
check("officer /departments 200", r.status_code == 200, str(r.status_code))
check("departments page lists seed dept", "Основной состав" in r.text)

r = client.post(
    "/departments",
    data={
        "csrf_token": "testcsrf",
        "name": "Отдел кадров",
        "description": "Кадры",
        "color": "#22d3ee",
        "role_id": "",
        "copy_from": "",
    },
    follow_redirects=False,
)
check("create department redirects", r.status_code == 303, str(r.status_code))
dept = next((d for d in db.list_departments() if d["name"] == "Отдел кадров"), None)
check("department in db", dept is not None)

r = client.post(
    "/departments",
    data={
        "csrf_token": "testcsrf",
        "name": "Резерв",
        "color": "#f59e0b",
        "copy_from": str(dept["id"]),
    },
    follow_redirects=False,
)
check("clone department redirects", r.status_code == 303, str(r.status_code))

r = client.post(
    f"/departments/{dept['id']}/delete",
    data={"csrf_token": "testcsrf"},
    follow_redirects=False,
)
check("delete department redirects", r.status_code == 303, str(r.status_code))
check("department gone", db.get_department(int(dept["id"])) is None)

# a rank created through the form lands in the chosen department
dept = next(d for d in db.list_departments() if d["name"] == "Резерв")
check("clone copied nothing from empty ladder", dept["rank_count"] == 0)
r = client.post(
    "/ranks",
    data={
        "csrf_token": "testcsrf",
        "name": "Стажёр",
        "role_id": "",
        "level": "0",
        "min_score": "0",
        "color": "#999999",
        "description": "",
        "department_id": str(dept["id"]),
    },
    follow_redirects=False,
)
check("create rank in department", r.status_code == 303, str(r.status_code))
intern = next((x for x in db.list_ranks(int(dept["id"])) if x["name"] == "Стажёр"), None)
check("rank bound to department", intern is not None, str([x["name"] for x in db.list_ranks()]))

# move a member through the department ladder
client.post(
    "/ranks",
    data={
        "csrf_token": "testcsrf",
        "name": "Мастер",
        "role_id": "",
        "level": "1",
        "min_score": "50",
        "color": "#22c55e",
        "description": "",
        "department_id": str(dept["id"]),
    },
    follow_redirects=False,
)
r = client.post(
    "/members/2002/department",
    data={"csrf_token": "testcsrf", "department_id": str(dept["id"])},
    follow_redirects=False,
)
check("assign department redirects", r.status_code == 303, str(r.status_code))
check("member has department", db.get_member(2002)["department_id"] == dept["id"])

r = client.post(
    "/members/2002/move",
    data={"csrf_token": "testcsrf", "direction": "up"},
    follow_redirects=False,
)
check("move up redirects", r.status_code == 303, str(r.status_code))
check(
    "member climbed ladder",
    db.get_member_full(2002)["rank_name"] in ("Стажёр", "Мастер"),
    str(db.get_member_full(2002)["rank_name"]),
)

r = client.get("/members/2002")
check("dossier shows department", "Резерв" in r.text)

# plain member cannot use department admin
session_for(3003, False, "Рядовой")
r = client.get("/departments", follow_redirects=False)
check("member /departments forbidden", r.status_code == 403, str(r.status_code))

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
