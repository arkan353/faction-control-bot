"""SQLite storage layer for ranks, members, marks and audit log."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from config import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS departments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    description TEXT    NOT NULL DEFAULT '',
    color       TEXT    NOT NULL DEFAULT '#5865f2',
    role_id     INTEGER,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ranks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    role_id       INTEGER,
    level         INTEGER NOT NULL DEFAULT 0,
    min_score     INTEGER NOT NULL DEFAULT 0,
    color         TEXT    NOT NULL DEFAULT '#6b7280',
    description   TEXT    NOT NULL DEFAULT '',
    department_id INTEGER REFERENCES departments(id) ON DELETE CASCADE,
    created_at    INTEGER NOT NULL,
    UNIQUE(department_id, name)
);

CREATE TABLE IF NOT EXISTS members (
    discord_id     INTEGER PRIMARY KEY,
    username       TEXT NOT NULL DEFAULT '',
    display_name   TEXT NOT NULL DEFAULT '',
    avatar         TEXT NOT NULL DEFAULT '',
    rank_id        INTEGER REFERENCES ranks(id) ON DELETE SET NULL,
    department_id  INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    score          INTEGER NOT NULL DEFAULT 0,
    callsign       TEXT NOT NULL DEFAULT '',
    note           TEXT NOT NULL DEFAULT '',
    active         INTEGER NOT NULL DEFAULT 1,
    officer        INTEGER NOT NULL DEFAULT 0,
    joined_at      INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS marks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER NOT NULL REFERENCES members(discord_id) ON DELETE CASCADE,
    title       TEXT    NOT NULL,
    comment     TEXT    NOT NULL DEFAULT '',
    delta       INTEGER NOT NULL DEFAULT 0,
    issued_by   INTEGER NOT NULL DEFAULT 0,
    issued_name TEXT    NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    revoked_at  INTEGER,
    revoked_by  TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_marks_member ON marks(member_id);
CREATE INDEX IF NOT EXISTS idx_members_rank ON members(rank_id);

CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id  INTEGER NOT NULL DEFAULT 0,
    actor     TEXT    NOT NULL DEFAULT '',
    action    TEXT    NOT NULL,
    target    TEXT    NOT NULL DEFAULT '',
    details   TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS marker_presets (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL UNIQUE,
    delta INTEGER NOT NULL
);
"""

# Indexes that rely on columns added by the department migration.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_ranks_dept ON ranks(department_id);
CREATE INDEX IF NOT EXISTS idx_members_dept ON members(department_id);
"""

# Rebuilt ranks table for databases created before departments existed.
RANKS_V2 = """
CREATE TABLE ranks_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    role_id       INTEGER,
    level         INTEGER NOT NULL DEFAULT 0,
    min_score     INTEGER NOT NULL DEFAULT 0,
    color         TEXT    NOT NULL DEFAULT '#6b7280',
    description   TEXT    NOT NULL DEFAULT '',
    department_id INTEGER REFERENCES departments(id) ON DELETE CASCADE,
    created_at    INTEGER NOT NULL,
    UNIQUE(department_id, name)
);
"""

DEFAULT_PRESETS = [
    ("Поощрение", 5),
    ("Отличная служба", 10),
    ("Выговор", -5),
    ("Строгий выговор", -10),
    ("Замечание", 0),
]

DEFAULT_RANKS = [
    ("Рекрут", 0, -1000, "#9ca3af"),
    ("Боец", 1, 0, "#60a5fa"),
    ("Ветеран", 2, 15, "#34d399"),
    ("Офицер", 3, 40, "#fbbf24"),
    ("Командир", 4, 80, "#f87171"),
]

DEFAULT_DEPARTMENT = ("Основной состав", "Общая иерархия фракции", "#5865f2")


@dataclass
class Actor:
    """Who performed an action: a web session user or a Discord admin."""

    id: int
    name: str
    is_officer: bool = True

    def __str__(self) -> str:
        return self.name


def now() -> int:
    return int(time.time())


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
    _migrate_departments()
    with connect() as conn:
        conn.executescript(INDEXES)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_departments() -> None:
    """Bring databases created before departments up to the current schema.

    Rebuilding a table needs foreign keys off, otherwise dropping the legacy
    ranks table would null out every member's rank through ON DELETE SET NULL.
    SCHEMA itself re-enables them, so the pragma is applied again right before
    the rebuild.
    """
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA foreign_keys=OFF")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
            raise RuntimeError("Не удалось отключить внешние ключи для миграции отделов")
        ranks_cols = _columns(conn, "ranks")
        members_cols = _columns(conn, "members")

        rank_count = int(conn.execute("SELECT COUNT(*) AS c FROM ranks").fetchone()["c"])
        dept_count = int(conn.execute("SELECT COUNT(*) AS c FROM departments").fetchone()["c"])

        legacy_ranks = "department_id" not in ranks_cols
        legacy_members = "department_id" not in members_cols
        if not legacy_ranks and not legacy_members:
            conn.execute("PRAGMA foreign_keys=ON")
            return

        conn.execute("BEGIN")
        default_dept: int | None = None
        if dept_count == 0 and rank_count > 0:
            cur = conn.execute(
                "INSERT INTO departments(name, description, color, created_at) VALUES(?,?,?,?)",
                (*DEFAULT_DEPARTMENT, now()),
            )
            default_dept = int(cur.lastrowid)
        elif dept_count:
            row = conn.execute("SELECT id FROM departments ORDER BY id LIMIT 1").fetchone()
            default_dept = int(row["id"])

        if legacy_ranks:
            orphaned = conn.execute(
                "SELECT COUNT(*) AS c FROM members m WHERE m.rank_id IS NOT NULL"
                " AND NOT EXISTS (SELECT 1 FROM ranks r WHERE r.id = m.rank_id)"
            ).fetchone()["c"]
            if orphaned:
                raise RuntimeError(f"В базе {orphaned} участник(ов) ссылаются на несуществующий ранг")
            conn.executescript(RANKS_V2)
            conn.execute(
                "INSERT INTO ranks_new(id, name, role_id, level, min_score, color,"
                " description, department_id, created_at)"
                " SELECT id, name, role_id, level, min_score, color, description, ?, created_at"
                " FROM ranks",
                (default_dept,),
            )
            conn.execute("DROP TABLE ranks")
            conn.execute("ALTER TABLE ranks_new RENAME TO ranks")
            lost = conn.execute(
                "SELECT COUNT(*) AS c FROM members m WHERE m.rank_id IS NOT NULL"
                " AND NOT EXISTS (SELECT 1 FROM ranks r WHERE r.id = m.rank_id)"
            ).fetchone()["c"]
            if lost:
                raise RuntimeError(f"Пересборка рангов потеряла {lost} привязок участников")

        if legacy_members:
            conn.execute(
                "ALTER TABLE members ADD COLUMN department_id INTEGER"
                " REFERENCES departments(id) ON DELETE SET NULL"
            )
            if default_dept is not None:
                conn.execute(
                    "UPDATE members SET department_id=? WHERE department_id IS NULL",
                    (default_dept,),
                )

        if default_dept is not None and not db_setting_exists(conn, "default_department_id"):
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('default_department_id', ?)",
                (str(default_dept),),
            )

        conn.commit()
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"Миграция отделов нарушила ссылки: {broken[:3]}")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()


def db_setting_exists(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone() is not None


# --------------------------------------------------------------------- departments
def list_departments() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT d.*,"
            " (SELECT COUNT(*) FROM ranks r WHERE r.department_id = d.id) AS rank_count,"
            " (SELECT COUNT(*) FROM members m WHERE m.department_id = d.id) AS member_count"
            " FROM departments d ORDER BY d.name COLLATE NOCASE"
        ).fetchall()


def get_department(department_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM departments WHERE id=?", (department_id,)
        ).fetchone()


def default_department_id() -> int | None:
    """Ladder used by members who have no department of their own."""
    raw = get_setting("default_department_id", "")
    if raw.isdigit() and get_department(int(raw)) is not None:
        return int(raw)
    with connect() as conn:
        row = conn.execute("SELECT id FROM departments ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def create_department(
    name: str,
    description: str = "",
    color: str = "#5865f2",
    role_id: int | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO departments(name, description, color, role_id, created_at)"
            " VALUES(?,?,?,?,?)",
            (name.strip(), description.strip(), color, role_id, now()),
        )
        return int(cur.lastrowid)


def update_department(department_id: int, **fields: Any) -> None:
    allowed = {"name", "description", "color", "role_id"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return
    values.append(department_id)
    with connect() as conn:
        conn.execute(f"UPDATE departments SET {', '.join(sets)} WHERE id=?", values)


def delete_department(department_id: int) -> None:
    """Drop the department with its rank ladder; members fall back to no department."""
    with connect() as conn:
        conn.execute("UPDATE members SET department_id=NULL WHERE department_id=?", (department_id,))
        conn.execute("DELETE FROM departments WHERE id=?", (department_id,))
    if get_setting("default_department_id", "") == str(department_id):
        set_setting("default_department_id", "")


def department_member_rows() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(department_id, 0) AS did, COUNT(*) AS c FROM members GROUP BY did"
        ).fetchall()
    return {str(r["did"]): int(r["c"]) for r in rows}


# --------------------------------------------------------------------------- ranks
def list_ranks(department_id: int | None = None) -> list[sqlite3.Row]:
    """Highest rank first (level descending), optionally within one department."""
    sql = (
        "SELECT r.*, d.name AS department_name, d.color AS department_color"
        " FROM ranks r LEFT JOIN departments d ON d.id = r.department_id"
    )
    params: list[Any] = []
    if department_id is not None:
        sql += " WHERE r.department_id = ?"
        params.append(department_id)
    sql += " ORDER BY d.name COLLATE NOCASE, r.level DESC, r.min_score DESC, r.name COLLATE NOCASE"
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def get_rank(rank_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM ranks WHERE id=?", (rank_id,)).fetchone()


def rank_ladder(department_id: int | None = None) -> list[sqlite3.Row]:
    """Lowest rank first, used for score-based resolution."""
    sql = "SELECT * FROM ranks"
    params: list[Any] = []
    if department_id is not None:
        sql += " WHERE department_id = ?"
        params.append(department_id)
    sql += " ORDER BY level ASC, min_score ASC, id ASC"
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def create_rank(
    name: str,
    role_id: int | None = None,
    level: int = 0,
    min_score: int = 0,
    color: str = "#6b7280",
    description: str = "",
    department_id: int | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO ranks(name, role_id, level, min_score, color, description,"
            " department_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                name.strip(),
                role_id,
                level,
                min_score,
                color,
                description.strip(),
                department_id,
                now(),
            ),
        )
        return int(cur.lastrowid)


def update_rank(rank_id: int, **fields: Any) -> None:
    allowed = {
        "name",
        "role_id",
        "level",
        "min_score",
        "color",
        "description",
        "department_id",
    }
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return
    values.append(rank_id)
    with connect() as conn:
        conn.execute(f"UPDATE ranks SET {', '.join(sets)} WHERE id=?", values)


def delete_rank(rank_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM ranks WHERE id=?", (rank_id,))


def resolve_rank_for_score(score: int, department_id: int | None = None) -> sqlite3.Row | None:
    """Pick the highest rank of the department whose min_score the member satisfies."""
    chosen = None
    for rank in rank_ladder(department_id):
        if score >= int(rank["min_score"]):
            chosen = rank
    return chosen


def next_rank_in_department(rank_id: int) -> sqlite3.Row | None:
    """Rank directly above the given one inside the same department."""
    rank = get_rank(rank_id)
    if rank is None:
        return None
    for candidate in rank_ladder(rank["department_id"]):
        if int(candidate["level"]) > int(rank["level"]) or (
            int(candidate["level"]) == int(rank["level"])
            and int(candidate["min_score"]) > int(rank["min_score"])
        ):
            return candidate
    return None


# -------------------------------------------------------------------------- members
def get_member(discord_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM members WHERE discord_id=?", (discord_id,)).fetchone()


def get_member_full(discord_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT m.*, r.name AS rank_name, r.color AS rank_color,"
            " r.level AS rank_level, r.role_id AS rank_role_id,"
            " d.name AS department_name, d.color AS department_color"
            " FROM members m"
            " LEFT JOIN ranks r ON r.id = m.rank_id"
            " LEFT JOIN departments d ON d.id = m.department_id"
            " WHERE m.discord_id=?",
            (discord_id,),
        ).fetchone()


def upsert_member(
    discord_id: int,
    username: str = "",
    display_name: str = "",
    avatar: str = "",
    rank_id: int | None = None,
    officer: bool | None = None,
) -> sqlite3.Row:
    """Create the member on first login, refreshing cached Discord profile fields."""
    existing = get_member(discord_id)
    ts = now()
    with connect() as conn:
        if existing is None:
            conn.execute(
                "INSERT INTO members(discord_id, username, display_name, avatar, rank_id,"
                " officer, joined_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    discord_id,
                    username,
                    display_name,
                    avatar,
                    rank_id,
                    1 if officer else 0,
                    ts,
                    ts,
                ),
            )
        else:
            conn.execute(
                "UPDATE members SET username=?, display_name=?, avatar=?, officer=?, updated_at=?"
                " WHERE discord_id=?",
                (
                    username or existing["username"],
                    display_name or existing["display_name"],
                    avatar or existing["avatar"],
                    (1 if officer else 0) if officer is not None else existing["officer"],
                    ts,
                    discord_id,
                ),
            )
    return get_member_full(discord_id)  # type: ignore[return-value]


def update_member(discord_id: int, **fields: Any) -> None:
    allowed = {
        "rank_id",
        "department_id",
        "callsign",
        "note",
        "active",
        "officer",
        "display_name",
        "username",
        "avatar",
    }
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return
    sets.append("updated_at=?")
    values.append(now())
    values.append(discord_id)
    with connect() as conn:
        conn.execute(f"UPDATE members SET {', '.join(sets)} WHERE discord_id=?", values)


def list_members(
    search: str = "",
    rank_id: int | None = None,
    department_id: int | None = None,
) -> list[sqlite3.Row]:
    sql = (
        "SELECT m.*, r.name AS rank_name, r.color AS rank_color, r.level AS rank_level,"
        " d.name AS department_name, d.color AS department_color"
        " FROM members m"
        " LEFT JOIN ranks r ON r.id = m.rank_id"
        " LEFT JOIN departments d ON d.id = m.department_id"
        " WHERE 1=1"
    )
    params: list[Any] = []
    if search:
        like = f"%{search}%"
        sql += " AND (m.display_name LIKE ? OR m.username LIKE ? OR m.callsign LIKE ?)"
        params += [like, like, like]
    if rank_id is not None:
        sql += " AND m.rank_id = ?"
        params.append(rank_id)
    if department_id is not None:
        sql += " AND m.department_id = ?"
        params.append(department_id)
    sql += (
        " ORDER BY COALESCE(r.level, -1) DESC, m.score DESC,"
        " m.display_name COLLATE NOCASE"
    )
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def recount_score(discord_id: int, conn: sqlite3.Connection | None = None) -> int:
    """Recompute the cached score from live (non-revoked) marks."""
    def _work(c: sqlite3.Connection) -> int:
        row = c.execute(
            "SELECT COALESCE(SUM(delta), 0) AS s FROM marks"
            " WHERE member_id=? AND revoked_at IS NULL",
            (discord_id,),
        ).fetchone()
        total = int(row["s"])
        c.execute(
            "UPDATE members SET score=?, updated_at=? WHERE discord_id=?",
            (total, now(), discord_id),
        )
        return total

    if conn is not None:
        return _work(conn)
    with connect() as c:
        return _work(c)


# ---------------------------------------------------------------------------- marks
def add_mark(
    member_id: int,
    title: str,
    comment: str,
    delta: int,
    issued_by: int,
    issued_name: str,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO marks(member_id, title, comment, delta, issued_by, issued_name,"
            " created_at) VALUES(?,?,?,?,?,?,?)",
            (member_id, title.strip(), comment.strip(), delta, issued_by, issued_name, now()),
        )
        recount_score(member_id, conn)
        return int(cur.lastrowid)


def get_mark(mark_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM marks WHERE id=?", (mark_id,)).fetchone()


def list_marks(member_id: int, include_revoked: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM marks WHERE member_id=?"
    if not include_revoked:
        sql += " AND revoked_at IS NULL"
    sql += " ORDER BY created_at DESC, id DESC"
    with connect() as conn:
        return conn.execute(sql, (member_id,)).fetchall()


def recent_marks(limit: int = 25) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT k.*, m.display_name AS member_name FROM marks k"
            " LEFT JOIN members m ON m.discord_id = k.member_id"
            " WHERE k.revoked_at IS NULL ORDER BY k.created_at DESC, k.id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def revoke_mark(mark_id: int, revoked_by: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT * FROM marks WHERE id=?", (mark_id,)).fetchone()
        if row is None or row["revoked_at"] is not None:
            return False
        conn.execute(
            "UPDATE marks SET revoked_at=?, revoked_by=? WHERE id=?",
            (now(), revoked_by, mark_id),
        )
        recount_score(int(row["member_id"]), conn)
        return True


def mark_stats(member_id: int) -> dict[str, int]:
    with connect() as conn:
        row = conn.execute(
            "SELECT"
            " SUM(CASE WHEN delta > 0 THEN 1 ELSE 0 END) AS pos,"
            " SUM(CASE WHEN delta < 0 THEN 1 ELSE 0 END) AS neg,"
            " SUM(CASE WHEN delta = 0 THEN 1 ELSE 0 END) AS neu,"
            " COALESCE(SUM(delta), 0) AS score"
            " FROM marks WHERE member_id=? AND revoked_at IS NULL",
            (member_id,),
        ).fetchone()
    return {
        "positive": int(row["pos"] or 0),
        "negative": int(row["neg"] or 0),
        "neutral": int(row["neu"] or 0),
        "score": int(row["score"] or 0),
    }


# ---------------------------------------------------------------------------- audit
def log_action(actor_id: int, actor: str, action: str, target: str = "", details: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit(actor_id, actor, action, target, details, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (actor_id, actor, action, target, details, now()),
        )


def recent_audit(limit: int = 40) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM audit ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()


# ------------------------------------------------------------------------- settings
def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def list_presets() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM marker_presets ORDER BY id").fetchall()


def add_preset(title: str, delta: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO marker_presets(title, delta) VALUES(?,?)"
            " ON CONFLICT(title) DO UPDATE SET delta=excluded.delta",
            (title.strip(), delta),
        )


def delete_preset(preset_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM marker_presets WHERE id=?", (preset_id,))


def member_rank_rows() -> dict[str, int]:
    """Member counts keyed by rank id (string keys for template lookups)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(rank_id, 0) AS rid, COUNT(*) AS c FROM members GROUP BY rid"
        ).fetchall()
    return {str(r["rid"]): int(r["c"]) for r in rows}


def department_rank_rows() -> dict[str, int]:
    """Member counts keyed by (department id, rank id) for the department ladders."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT department_id AS did, COALESCE(rank_id, 0) AS rid, COUNT(*) AS c"
            " FROM members GROUP BY department_id, rank_id"
        ).fetchall()
    out: dict[str, int] = {}
    for row in rows:
        key = f"{row['did'] or 0}:{row['rid']}"
        out[key] = int(row["c"])
    return out


def seed_defaults() -> None:
    """Seed the rank ladder and mark presets into a fresh database."""
    with connect() as conn:
        has_depts = conn.execute("SELECT COUNT(*) AS c FROM departments").fetchone()["c"]
        has_ranks = conn.execute("SELECT COUNT(*) AS c FROM ranks").fetchone()["c"]
        if not has_depts and not has_ranks:
            dept_id = insert_department(conn, *DEFAULT_DEPARTMENT)
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('default_department_id', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(dept_id),),
            )
        elif has_depts:
            row = conn.execute("SELECT id FROM departments ORDER BY id LIMIT 1").fetchone()
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('default_department_id', ?)"
                " ON CONFLICT(key) DO NOTHING",
                (str(row["id"]),),
            )

        if not has_ranks:
            dept_row = conn.execute("SELECT id FROM departments ORDER BY id LIMIT 1").fetchone()
            dept_id = int(dept_row["id"]) if dept_row else None
            ts = now()
            for name, level, min_score, color in DEFAULT_RANKS:
                conn.execute(
                    "INSERT INTO ranks(name, level, min_score, color, description,"
                    " department_id, created_at) VALUES(?,?,?,?,?,?,?)",
                    (name, level, min_score, color, "Стартовый ранг", dept_id, ts),
                )
        has_presets = conn.execute("SELECT COUNT(*) AS c FROM marker_presets").fetchone()["c"]
        if not has_presets:
            for title, delta in DEFAULT_PRESETS:
                conn.execute(
                    "INSERT INTO marker_presets(title, delta) VALUES(?,?)", (title, delta)
                )


def insert_department(conn: sqlite3.Connection, name: str, description: str, color: str) -> int:
    cur = conn.execute(
        "INSERT INTO departments(name, description, color, created_at) VALUES(?,?,?,?)",
        (name, description, color, now()),
    )
    return int(cur.lastrowid)
