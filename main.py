"""FastAPI web panel: Discord OAuth login, personal dossiers, rank management."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth
import core
import db
import filters
from config import (
    ADMIN_IDS,
    BASE_DIR,
    BASE_URL,
    OFFICER_ROLE_IDS,
    oauth_configured,
)

log = logging.getLogger("faction.web")

FLASH_COOKIE = "faction_flash"

app = FastAPI(title="Faction Control", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["dt"] = filters.dt
templates.env.filters["short_date"] = filters.short_date


# --------------------------------------------------------------------- helpers
def _safe_next(value: str | None) -> str:
    """Only allow same-origin relative redirects."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return "/"
    return value


def _current(request: Request) -> tuple[dict[str, Any] | None, auth.SessionUser | None]:
    payload = auth.loads(request.cookies.get(auth.SESSION_COOKIE))
    if payload is None:
        return None, None
    return payload, auth.session_user(payload)


def _is_officer(payload: dict[str, Any] | None) -> bool:
    if payload is None:
        return False
    discord_id = int(payload["discord_id"])
    if discord_id in ADMIN_IDS:
        return True
    row = db.get_member(discord_id)
    return bool(row and row["officer"])


def _require_officer(request: Request) -> dict[str, Any]:
    payload, user = _current(request)
    if user is None:
        raise HTTPException(status_code=401)
    if not _is_officer(payload):
        raise HTTPException(status_code=403)
    return payload  # type: ignore[return-value]


def _csrf_ok(request: Request, token: str) -> bool:
    payload, _ = _current(request)
    return auth.check_csrf(payload, token)


def _actor(user: auth.SessionUser) -> db.Actor:
    return db.Actor(id=user.discord_id, name=user.display_name or user.username)


def _render(
    request: Request,
    template: str,
    *,
    user: auth.SessionUser | None,
    officer: bool,
    status_code: int = 200,
    csrf: str = "",
    **context: Any,
) -> HTMLResponse:
    flash = unquote(request.cookies.get(FLASH_COOKIE, ""))
    response = templates.TemplateResponse(
        request,
        template,
        {
            "user": user,
            "officer": officer,
            "faction": core.faction_name(),
            "csrf": csrf or (user.csrf if user else ""),
            "flash": flash,
            **context,
        },
        status_code=status_code,
    )
    if flash:
        response.delete_cookie(FLASH_COOKIE)
    return response


def _redirect_with_flash(url: str, message: str) -> RedirectResponse:
    """Redirect and show a one-shot notice on the next page render."""
    resp = RedirectResponse(url, status_code=303)
    if message:
        resp.set_cookie(
            FLASH_COOKIE, quote(message)[:600], max_age=30, httponly=True, samesite="lax"
        )
    return resp


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' https://cdn.discordapp.com data:;"
        " style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
    )
    return response


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    db.seed_defaults()
    if not oauth_configured():
        log.warning("OAuth не настроен: CLIENT_ID/CLIENT_SECRET отсутствуют в .env")


# ------------------------------------------------------------------------ auth
@app.get("/login", response_class=HTMLResponse)
async def login(request: Request, next: str = "/"):
    if not oauth_configured():
        return _render(
            request,
            "login.html",
            user=None,
            officer=False,
            configured=False,
            error="Discord OAuth не настроен: добавьте CLIENT_ID и CLIENT_SECRET в .env",
        )
    state = auth.new_state()
    resp = RedirectResponse(auth.authorize_url(state), status_code=302)
    resp.set_cookie(auth.STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax")
    resp.set_cookie(
        auth.NEXT_COOKIE, _safe_next(next), max_age=600, httponly=True, samesite="lax"
    )
    return resp


@app.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return _render(
            request,
            "login.html",
            user=None,
            officer=False,
            configured=True,
            error="Вход отменён или код не получен.",
        )
    expected_state = request.cookies.get(auth.STATE_COOKIE, "")
    if not expected_state or state != expected_state:
        return _render(
            request,
            "login.html",
            user=None,
            officer=False,
            configured=True,
            error="Сессия входа истекла. Попробуйте снова.",
        )

    try:
        profile, access_token = await auth.exchange_code(code)
    except httpx.HTTPStatusError as exc:
        log.warning(
            "OAuth обмен не удался: %s %s", exc.response.status_code, exc.response.text[:300]
        )
        return _render(
            request,
            "login.html",
            user=None,
            officer=False,
            configured=True,
            error="Discord отклонил вход. Проверьте redirect URI и CLIENT_SECRET.",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("OAuth ошибка")
        return _render(
            request,
            "login.html",
            user=None,
            officer=False,
            configured=True,
            error=f"Ошибка входа: {exc}",
        )

    discord_id = int(profile["id"])
    payload = auth.make_session_payload(profile)

    # None = "cannot determine now", so an existing officer flag is preserved.
    officer: bool | None = True if discord_id in ADMIN_IDS else None
    if officer is None and OFFICER_ROLE_IDS:
        guild_id = db.get_setting("guild_id", "")
        if guild_id.isdigit():
            roles = await auth.fetch_guild_roles(access_token, int(guild_id))
            if roles:
                officer = bool(set(roles) & OFFICER_ROLE_IDS)

    is_new = db.get_member(discord_id) is None
    db.upsert_member(
        discord_id,
        username=payload["username"],
        display_name=payload["display_name"],
        avatar=payload["avatar"],
        officer=officer,
    )
    if is_new and core.auto_rank_enabled():
        await core.recalc_member_rank(discord_id)

    target = _safe_next(request.cookies.get(auth.NEXT_COOKIE))
    resp = RedirectResponse(target, status_code=302)
    resp.set_cookie(
        auth.SESSION_COOKIE,
        auth.dumps(payload),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    resp.delete_cookie(auth.NEXT_COOKIE)
    resp.delete_cookie(auth.STATE_COOKIE)
    return resp


@app.post("/logout")
async def logout(request: Request, csrf_token: str = Form("")):
    resp = RedirectResponse("/login", status_code=303)
    if _csrf_ok(request, csrf_token):
        resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


# -------------------------------------------------------------------- overview
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    payload, user = _current(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    officer = _is_officer(payload)

    me = db.get_member_full(user.discord_id)
    ranks = db.list_ranks()
    return _render(
        request,
        "index.html",
        user=user,
        officer=officer,
        me=me,
        stats=db.mark_stats(user.discord_id),
        progress=core.ladder_progress(int(me["score"]) if me else 0, ranks),
        marks=db.list_marks(user.discord_id)[:6],
        ranks=ranks,
        recent=db.recent_marks(8) if officer else [],
        member_count=len(db.list_members()),
        audit=db.recent_audit(10) if officer else [],
    )


@app.get("/me")
async def my_file(request: Request):
    _, user = _current(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse(f"/members/{user.discord_id}", status_code=303)


# --------------------------------------------------------------------- members
@app.get("/members", response_class=HTMLResponse)
async def members(request: Request, q: str = "", rank: str = ""):
    payload, user = _current(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not _is_officer(payload):
        raise HTTPException(status_code=403)

    rank_id = int(rank) if rank.isdigit() else None
    rows = db.list_members(search=q.strip(), rank_id=rank_id)
    return _render(
        request,
        "members.html",
        user=user,
        officer=True,
        members=rows,
        ranks=db.list_ranks(),
        q=q,
        selected_rank=rank,
    )


@app.get("/members/{discord_id}", response_class=HTMLResponse)
async def member_file(request: Request, discord_id: int):
    payload, user = _current(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    officer = _is_officer(payload)
    if discord_id != user.discord_id and not officer:
        raise HTTPException(status_code=403)

    row = db.get_member_full(discord_id)
    if row is None and officer:
        db.upsert_member(discord_id, display_name=f"Участник {discord_id}")
        row = db.get_member_full(discord_id)
    if row is None:
        raise HTTPException(status_code=404)

    ranks = db.list_ranks()
    return _render(
        request,
        "member.html",
        user=user,
        officer=officer,
        m=row,
        marks=db.list_marks(discord_id, include_revoked=officer),
        ranks=ranks,
        stats=db.mark_stats(discord_id),
        progress=core.ladder_progress(int(row["score"]), ranks),
        presets=db.list_presets(),
        is_self=int(row["discord_id"]) == user.discord_id,
    )


@app.post("/members/{discord_id}/marks")
async def create_mark(
    request: Request,
    discord_id: int,
    csrf_token: str = Form(""),
    title: str = Form(""),
    delta: str = Form("0"),
    comment: str = Form(""),
):
    payload, user = _current(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not _is_officer(payload):
        raise HTTPException(status_code=403)
    if not _csrf_ok(request, csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")
    if not title.strip():
        raise HTTPException(status_code=400, detail="Название отметки обязательно")
    try:
        delta_val = int(delta)
    except ValueError:
        delta_val = 0
    delta_val = max(-100, min(100, delta_val))

    try:
        result = await core.issue_mark(
            discord_id, title.strip()[:100], comment, delta_val, _actor(user)
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    notice = ""
    if result.role_error:
        notice = f"Отметка записана, но роль в Discord не обновлена: {result.role_error}"
    elif result.promoted and result.new_rank is not None:
        notice = f"Повышение: {result.new_rank['name']}"
    elif result.demoted and result.new_rank is not None:
        notice = f"Понижение: {result.new_rank['name']}"
    return _redirect_with_flash(f"/members/{discord_id}", notice)


@app.post("/marks/{mark_id}/revoke")
async def revoke_mark(request: Request, mark_id: int, csrf_token: str = Form("")):
    payload, user = _current(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not _is_officer(payload):
        raise HTTPException(status_code=403)
    if not _csrf_ok(request, csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")

    mark = db.get_mark(mark_id)
    await core.revoke_mark(mark_id, _actor(user))
    back = f"/members/{mark['member_id']}" if mark else "/"
    return RedirectResponse(back, status_code=303)


@app.post("/members/{discord_id}/rank")
async def set_rank(
    request: Request,
    discord_id: int,
    csrf_token: str = Form(""),
    rank_id: str = Form(""),
):
    payload, user = _current(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not _is_officer(payload):
        raise HTTPException(status_code=403)
    if not _csrf_ok(request, csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")

    rid = int(rank_id) if rank_id.strip().isdigit() else None
    err = await core.set_member_rank(discord_id, rid, _actor(user))
    notice = f"Ранг назначен, но роль в Discord не обновлена: {err}" if err else "Ранг обновлён"
    return _redirect_with_flash(f"/members/{discord_id}", notice)


@app.post("/members/{discord_id}/profile")
async def update_profile(
    request: Request,
    discord_id: int,
    csrf_token: str = Form(""),
    callsign: str = Form(""),
    note: str = Form(""),
):
    payload, user = _current(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    officer = _is_officer(payload)
    if not officer and discord_id != user.discord_id:
        raise HTTPException(status_code=403)
    if not _csrf_ok(request, csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")

    fields: dict[str, Any] = {"callsign": callsign.strip()[:64]}
    if officer:
        fields["note"] = note.strip()[:2000]
    db.update_member(discord_id, **fields)
    db.log_action(
        user.discord_id,
        user.display_name or user.username,
        "profile.update",
        target=str(discord_id),
        details=f"позывной={fields['callsign']}",
    )
    return RedirectResponse(f"/members/{discord_id}", status_code=303)


# ------------------------------------------------------------------------ ranks
@app.get("/ranks", response_class=HTMLResponse)
async def ranks_page(request: Request):
    payload, user = _current(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not _is_officer(payload):
        raise HTTPException(status_code=403)
    return _render(
        request,
        "ranks.html",
        user=user,
        officer=True,
        ranks=db.list_ranks(),
        counts=db.member_rank_rows(),
        auto_rank=core.auto_rank_enabled(),
        presets=db.list_presets(),
    )


@app.post("/ranks")
async def rank_create(
    request: Request,
    csrf_token: str = Form(""),
    name: str = Form(""),
    role_id: str = Form(""),
    level: str = Form("0"),
    min_score: str = Form("0"),
    color: str = Form("#6b7280"),
    description: str = Form(""),
):
    payload, user = _current(request)
    if user is None or not _is_officer(payload):
        raise HTTPException(status_code=403)
    if not _csrf_ok(request, csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")
    if not name.strip():
        raise HTTPException(status_code=400, detail="Название обязательно")
    try:
        db.create_rank(
            name=name.strip()[:80],
            role_id=int(role_id) if role_id.strip().isdigit() else None,
            level=int(level or 0),
            min_score=int(min_score or 0),
            color=color if color.startswith("#") else f"#{color}",
            description=description[:300],
        )
    except Exception as exc:  # noqa: BLE001 - unique name constraint
        raise HTTPException(status_code=400, detail=f"Не удалось создать ранг: {exc}") from exc
    db.log_action(user.discord_id, user.display_name or user.username, "rank.create", target=name)
    return RedirectResponse("/ranks", status_code=303)


@app.post("/ranks/{rank_id}")
async def rank_edit(
    request: Request,
    rank_id: int,
    csrf_token: str = Form(""),
    name: str = Form(""),
    role_id: str = Form(""),
    level: str = Form("0"),
    min_score: str = Form("0"),
    color: str = Form("#6b7280"),
    description: str = Form(""),
):
    payload, user = _current(request)
    if user is None or not _is_officer(payload):
        raise HTTPException(status_code=403)
    if not _csrf_ok(request, csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")
    if db.get_rank(rank_id) is None:
        raise HTTPException(status_code=404)
    try:
        db.update_rank(
            rank_id,
            name=name.strip()[:80],
            role_id=int(role_id) if role_id.strip().isdigit() else None,
            level=int(level or 0),
            min_score=int(min_score or 0),
            color=color if color.startswith("#") else f"#{color}",
            description=description[:300],
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Не удалось сохранить: {exc}") from exc
    db.log_action(user.discord_id, user.display_name or user.username, "rank.edit", target=str(rank_id))
    return RedirectResponse("/ranks", status_code=303)


@app.post("/ranks/{rank_id}/delete")
async def rank_delete(request: Request, rank_id: int, csrf_token: str = Form("")):
    payload, user = _current(request)
    if user is None or not _is_officer(payload):
        raise HTTPException(status_code=403)
    if not _csrf_ok(request, csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")
    rank = db.get_rank(rank_id)
    db.delete_rank(rank_id)
    db.log_action(
        user.discord_id,
        user.display_name or user.username,
        "rank.delete",
        target=str(rank["name"]) if rank else str(rank_id),
    )
    return RedirectResponse("/ranks", status_code=303)


@app.post("/presets")
async def preset_create(
    request: Request,
    csrf_token: str = Form(""),
    title: str = Form(""),
    delta: str = Form("0"),
):
    payload, user = _current(request)
    if user is None or not _is_officer(payload):
        raise HTTPException(status_code=403)
    if not _csrf_ok(request, csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")
    if title.strip():
        try:
            db.add_preset(title.strip()[:60], max(-100, min(100, int(delta or 0))))
        except ValueError:
            pass
    return RedirectResponse("/ranks", status_code=303)


@app.post("/presets/{preset_id}/delete")
async def preset_delete(request: Request, preset_id: int, csrf_token: str = Form("")):
    payload, user = _current(request)
    if user is None or not _is_officer(payload):
        raise HTTPException(status_code=403)
    if not _csrf_ok(request, csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")
    db.delete_preset(preset_id)
    return RedirectResponse("/ranks", status_code=303)


# --------------------------------------------------------------------- settings
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    payload, user = _current(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not _is_officer(payload):
        raise HTTPException(status_code=403)
    return _render(
        request,
        "settings.html",
        user=user,
        officer=True,
        auto_rank=core.auto_rank_enabled(),
        guild_name=db.get_setting("guild_name", "—"),
        guild_id=db.get_setting("guild_id", "—"),
        base_url=BASE_URL,
        oauth_ok=oauth_configured(),
        audit=db.recent_audit(60),
    )


@app.post("/settings")
async def settings_save(
    request: Request,
    csrf_token: str = Form(""),
    faction_name: str = Form(""),
    auto_rank: str = Form(""),
):
    payload, user = _current(request)
    if user is None or not _is_officer(payload):
        raise HTTPException(status_code=403)
    if not _csrf_ok(request, csrf_token):
        raise HTTPException(status_code=400, detail="CSRF")
    if faction_name.strip():
        db.set_setting("faction_name", faction_name.strip()[:64])
    db.set_setting("auto_rank", "1" if auto_rank == "on" else "0")
    db.log_action(
        user.discord_id,
        user.display_name or user.username,
        "settings.update",
        details=f"auto_rank={auto_rank}",
    )
    return RedirectResponse("/settings", status_code=303)


@app.get("/health")
async def health():
    return {"status": "ok", "faction": core.faction_name()}


@app.exception_handler(403)
async def forbidden(request: Request, exc: Exception):
    payload, user = _current(request)
    return _render(
        request,
        "error.html",
        user=user,
        officer=_is_officer(payload),
        status_code=403,
        code="403",
        message="Доступ только для офицеров фракции.",
    )


@app.exception_handler(404)
async def not_found(request: Request, exc: Exception):
    payload, user = _current(request)
    return _render(
        request,
        "error.html",
        user=user,
        officer=_is_officer(payload),
        status_code=404,
        code="404",
        message="Страница не найдена.",
    )
