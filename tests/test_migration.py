"""Check that a pre-departments database migrates without losing data."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DB_FILE = BASE / "data" / "test_migration.db"
for suffix in ("", "-wal", "-shm"):
    Path(str(DB_FILE) + suffix).unlink(missing_ok=True)

# --- build the legacy schema exactly as it shipped before departments ----------
LEGACY = """
PRAGMA journal_mode=WAL;
CREATE TABLE ranks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    role_id INTEGER,
    level INTEGER NOT NULL DEFAULT 0,
    min_score INTEGER NOT NULL DEFAULT 0,
    color TEXT NOT NULL DEFAULT '#6b7280',
    description TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE members (
    discord_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    avatar TEXT NOT NULL DEFAULT '',
    rank_id INTEGER REFERENCES ranks(id) ON DELETE SET NULL,
    score INTEGER NOT NULL DEFAULT 0,
    callsign TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    officer INTEGER NOT NULL DEFAULT 0,
    joined_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
legacy = sqlite3.connect(DB_FILE)
legacy.executescript(LEGACY)
legacy.execute(
    "INSERT INTO ranks(id, name, role_id, level, min_score, color, description, created_at)"
    " VALUES(1,'Рекрут',NULL,0,-1000,'#9ca3af','',1),"
    " (2,'Боец',NULL,1,0,'#60a5fa','',1),"
    " (3,'Ветеран',777,2,15,'#34d399','старый',1)"
)
legacy.execute(
    "INSERT INTO members(discord_id, username, display_name, rank_id, score, joined_at, updated_at)"
    " VALUES(9001,'a#1','Старый Боец',2,20,1,1),(9002,'b#1','Без ранга',NULL,0,1,1)"
)
legacy.execute("INSERT INTO settings(key, value) VALUES('faction_name','Старая Фракция')")
legacy.commit()
legacy.close()

failures: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + label + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        failures.append(label)


os.environ["DB_PATH"] = str(DB_FILE)
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SESSION_SECRET", "test-secret")
sys.path.insert(0, str(BASE))

import db  # noqa: E402

db.init_db()
db.seed_defaults()

with sqlite3.connect(DB_FILE) as conn:
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ranks)")}
    mcols = {r["name"] for r in conn.execute("PRAGMA table_info(members)")}
    broken = conn.execute("PRAGMA foreign_key_check").fetchall()

check("ranks gained department_id", "department_id" in cols, str(cols))
check("members gained department_id", "department_id" in mcols, str(mcols))
check("no broken foreign keys", not broken, str(broken))

depts = db.list_departments()
check("one seeding department created", len(depts) == 1, str([d["name"] for d in depts]))
dept_id = int(depts[0]["id"])

ranks = {r["name"]: r for r in db.list_ranks(dept_id)}
check("all legacy ranks kept", set(ranks) >= {"Рекрут", "Боец", "Ветеран"}, str(list(ranks)))
check("legacy rank role kept", ranks["Ветеран"]["role_id"] == 777, str(ranks["Ветеран"]["role_id"]))
check("legacy rank description kept", ranks["Ветеран"]["description"] == "старый")
check("legacy rank level kept", ranks["Ветеран"]["level"] == 2)

member = db.get_member_full(9001)
check("member rank survived", member["rank_id"] == ranks["Боец"]["id"], str(member["rank_id"]))
check("member moved into department", member["department_id"] == dept_id)
check("member score survived", member["score"] == 20, str(member["score"]))
check("member joined department name", member["department_name"] == depts[0]["name"])
check("unranked member kept", db.get_member(9002) is not None)
check("settings survived", db.get_setting("faction_name") == "Старая Фракция")
check("default department recorded", db.default_department_id() == dept_id)

# migration is idempotent: a second boot changes nothing
before = [(r["id"], r["name"], r["department_id"]) for r in db.list_ranks()]
db.init_db()
db.seed_defaults()
after = [(r["id"], r["name"], r["department_id"]) for r in db.list_ranks()]
check("second migration is a no-op", before == after, f"{before} != {after}")

# ladder resolution is scoped to the department it was asked about
check(
    "score resolves inside department",
    db.resolve_rank_for_score(20, dept_id)["name"] == "Ветеран",
    str(db.resolve_rank_for_score(20, dept_id)["name"]),
)
other = db.create_department("Технический", "Техника")
check(
    "new department has empty ladder",
    db.resolve_rank_for_score(20, other) is None,
    str(db.resolve_rank_for_score(20, other)),
)
db.create_rank(name="Механик", level=0, min_score=0, department_id=other)
db.create_rank(name="Старший механик", level=1, min_score=10, department_id=other)
check(
    "score resolves in own ladder",
    db.resolve_rank_for_score(20, other)["name"] == "Старший механик",
)
check("main ladder unaffected", db.resolve_rank_for_score(20, dept_id)["name"] == "Ветеран")

# deletion cascades the ladder but frees the members
db.update_member(9001, department_id=other)
db.delete_department(other)
check("department ranks removed", not db.list_ranks(other), str(db.list_ranks(other)))
check("member freed from deleted department", db.get_member(9001)["department_id"] is None)
check("member kept", db.get_member(9001) is not None)

for suffix in ("", "-wal", "-shm"):
    Path(str(DB_FILE) + suffix).unlink(missing_ok=True)

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
