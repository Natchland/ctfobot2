"""
FastAPI control-panel for CTFO bot
──────────────────────────────────
• Runs next to the Discord bot but as a separate Railway service.
• Importing ctfobot2_0 is safe because the bot starts only when
  botmod.main() is invoked (never at mere import).
"""

from __future__ import annotations

import os, json, datetime, asyncio, inspect, httpx, asyncpg
from pathlib import Path
from typing import Callable, Awaitable, Any

from itsdangerous import URLSafeSerializer, BadSignature
from passlib.context import CryptContext
from asyncpg import UniqueViolationError

from fastapi import (
    FastAPI,
    Request,
    Form,
    Response,
    HTTPException,
)
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import discord

# ─────────────────────────────────────────────────────────────
#  BOT  (import-safe thanks to guard in ctfobot2_0.py)
# ─────────────────────────────────────────────────────────────
import ctfobot2_0 as botmod

BOT_TOKEN = botmod.BOT_TOKEN
GUILD_ID  = botmod.GUILD_ID

# ─────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required for the web service")

WEB_SECRET  = os.getenv("WEB_SECRET", "CHANGE_ME")
OWNER_KEY   = os.getenv("OWNER_KEY",  "OWNER_ONLY")
COOKIE_NAME = "ctfo_admin"

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
signer  = URLSafeSerializer(WEB_SECRET)

# ─────────────────────────────────────────────────────────────
#  FASTAPI
# ─────────────────────────────────────────────────────────────
app       = FastAPI(debug=False)
templates = Jinja2Templates(directory="templates")

static_path = Path("static")
if static_path.is_dir():
    app.mount("/static", StaticFiles(directory=static_path), name="static")

db: asyncpg.Pool | None = None        # initialised on startup

# ═════════════════════════════  HELPERS  ══════════════════════════════
async def current_user(request: Request) -> str | None:
    """
    Return username stored in signed cookie **if** that user exists in DB
    and is approved; otherwise None.
    """
    if db is None:
        return None

    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        username = signer.loads(token)
    except BadSignature:
        return None

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT username, approved FROM admins WHERE username=$1",
            username
        )
    return row["username"] if row and row["approved"] else None


def login_required(fn: Callable[..., Awaitable[Any]]):
    """
    Decorator that
      • verifies cookie,
      • redirects unauthenticated users to /login,
      • injects a `user` argument (str) into the endpoint.
    """
    sig      = inspect.signature(fn)
    params   = list(sig.parameters.values())

    async def wrapper(request: Request, *args, **kwargs):   # type: ignore
        user = await current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        return await fn(request, user, *args, **kwargs)

    wrapper.__name__      = fn.__name__
    wrapper.__doc__       = fn.__doc__
    wrapper.__signature__ = inspect.Signature(
        parameters=[p for p in params if p.name != "user"]
    )
    return wrapper


def _build_role_list(guild: discord.Guild, data: dict[str, str]):
    roles: list[discord.Role] = []
    if (r := guild.get_role(botmod.ACCEPT_ROLE_ID)):                               roles.append(r)
    if (r := guild.get_role(botmod.REGION_ROLE_IDS.get(data.get("region"), 0))):   roles.append(r)
    if (r := guild.get_role(botmod.FOCUS_ROLE_IDS.get(data.get("focus"), 0))):     roles.append(r)
    return roles

# ═════════════════════════════  START-UP  ═════════════════════════════
@app.on_event("startup")
async def init_database():
    global db
    db = await asyncpg.create_pool(DATABASE_URL)
    async with db.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            username TEXT PRIMARY KEY,
            pwd_hash TEXT NOT NULL,
            approved BOOLEAN NOT NULL DEFAULT FALSE
        );""")
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS codes (
            name TEXT PRIMARY KEY,
            pin  TEXT NOT NULL,
            public BOOLEAN NOT NULL DEFAULT FALSE
        );""")
    print("[web] DB pool ready")


@app.on_event("startup")
async def launch_discord_bot():
    """
    Start the Discord bot in *this* process **only if** BOT_TOKEN is set.
    botmod.main() is synchronous; it schedules the async start coroutine
    on the already-running FastAPI loop.
    """
    if BOT_TOKEN:
        botmod.main()                        # no create_task needed
        print("[web] Discord bot task scheduled")


@app.on_event("shutdown")
async def stop_discord_bot():
    if not botmod.bot.is_closed():
        await botmod.bot.close()
        print("[web] Discord bot stopped")

# ═════════════════════════════  DATA QUERIES  ═════════════════════════
async def all_admin_data():
    """Fetch codes, member_forms, giveaways for the admin dashboard."""
    async with db.acquire() as conn:
        codes = await conn.fetch("SELECT * FROM codes ORDER BY name")
        forms = await conn.fetch(
            "SELECT * FROM member_forms ORDER BY created_at DESC"
        )
        gws   = await conn.fetch(
            "SELECT * FROM giveaways ORDER BY end_ts DESC"
        )

    forms_parsed = []
    for rec in forms:
        d = dict(rec)
        if isinstance(d["data"], str):
            try:
                d["data"] = json.loads(d["data"])
            except json.JSONDecodeError:
                d["data"] = {}
        forms_parsed.append(d)
    return codes, forms_parsed, gws

# ═════════════════════════════  PUBLIC PAGE  ══════════════════════════
@app.get("/", response_class=HTMLResponse)
async def welcome(request: Request):
    """Landing page that shows live guild member count via widget."""
    member_count = "?"
    try:
        async with httpx.AsyncClient() as cli:
            r = await cli.get(
                f"https://discord.com/api/guilds/{GUILD_ID}/widget.json",
                timeout=5
            )
            if r.status_code == 200:
                member_count = len(r.json()["members"])
    except Exception:
        pass

    return templates.TemplateResponse(
        "welcome.html",
        {
            "request": request,
            "year": datetime.datetime.now().year,
            "members": member_count,
        },
    )

# ═════════════════════════════  ADMIN PANEL  ══════════════════════════
@app.get("/admin", response_class=HTMLResponse)
@login_required
async def admin_panel(request: Request, user: str):
    codes, forms, gws = await all_admin_data()
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "codes": codes,
            "forms": forms,
            "gws":   gws,
            "user":  user,
            "year":  datetime.datetime.now().year,
        },
    )

# ═════════════════════════════  SIGN-UP / LOGIN  ══════════════════════
async def _get_admin_row(username: str):
    async with db.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM admins WHERE username=$1", username
        )

@app.get("/signup", response_class=HTMLResponse)
async def signup_get(request: Request):
    return templates.TemplateResponse("signup.html", {"request": request})


@app.post("/signup")
async def signup_post(username: str = Form(...), password: str = Form(...)):
    hash_ = pwd_ctx.hash(password)
    async with db.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO admins (username, pwd_hash) VALUES ($1,$2)",
                username, hash_
            )
        except UniqueViolationError:
            raise HTTPException(400, "Username already exists.")
    return RedirectResponse("/login?pending=1", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request, pending: int | None = None):
    return templates.TemplateResponse(
        "login.html", {"request": request, "pending": pending}
    )

@app.post("/login")
async def login_post(
    response: Response,
    username: str = Form(...),
    password: str = Form(...)
):
    row = await _get_admin_row(username)
    if (not row
            or not row["approved"]
            or not pwd_ctx.verify(password, row["pwd_hash"])):
        raise HTTPException(403, "Invalid credentials or not yet approved.")

    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        COOKIE_NAME, signer.dumps(username),
        httponly=True, max_age=7 * 86400
    )
    return resp

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp

# ═══════════════════════════  OWNER-ONLY ENDPOINT  ════════════════════
@app.post("/approve")
async def approve_user(request: Request, username: str = Form(...)):
    if request.headers.get("X-OWNER-KEY") != OWNER_KEY:
        raise HTTPException(403, "Bad owner key.")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE admins SET approved=TRUE WHERE username=$1", username
        )
    return "approved"

# ═════════════════════════════  CODE MANAGEMENT  ══════════════════════
@app.post("/codes/add")
@login_required
async def add_code(
    request: Request,
    user: str,
    name: str = Form(...),
    pin: str  = Form(...),
    public: str | None = Form(None)
):
    if not (pin.isdigit() and len(pin) == 4):
        raise HTTPException(400, "Pin must be 4 digits.")
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO codes (name, pin, public)
            VALUES ($1,$2,$3)
            ON CONFLICT(name) DO UPDATE SET pin=$2, public=$3
        """, name, pin, public is not None)
    return RedirectResponse("/admin", status_code=303)


@app.post("/codes/remove")
@login_required
async def remove_code(request: Request, user: str, name: str = Form(...)):
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM codes WHERE name=$1", name)
    return RedirectResponse("/admin", status_code=303)

# ═════════════════════════════  MEMBER-FORM CRUD  ═════════════════════
@app.post("/forms/update")
@login_required
async def update_form(
    request: Request,
    user: str,
    id: int = Form(...),
    json_text: str = Form(..., alias="json")
):
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError:
        raise HTTPException(400, "Not valid JSON.")

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE member_forms SET data=$2 WHERE id=$1",
            id, parsed
        )
    return JSONResponse({"status": "updated"})


@app.post("/forms/accept")
@login_required
async def accept_member(request: Request, user: str, id: int = Form(...)):
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, data, status FROM member_forms WHERE id=$1", id
        )
    if not row or row["status"] != "pending":
        raise HTTPException(400, "Form not found or already handled")

    data: dict = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
    uid: int   = row["user_id"]

    guild = botmod.bot.get_guild(GUILD_ID)
    if not guild:
        raise HTTPException(503, "Discord bot not ready")

    try:
        member = await guild.fetch_member(uid)
    except discord.NotFound:
        raise HTTPException(404, "User left the guild")

    roles = _build_role_list(guild, data)
    if not roles:
        raise HTTPException(500, "Required roles missing in guild")
    await member.add_roles(*roles, reason=f"Accepted via web panel ({user})")

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE member_forms SET status='accepted' WHERE id=$1", id
        )
    return JSONResponse({"status": "accepted"})


@app.post("/forms/deny")
@login_required
async def deny_member(request: Request, user: str, id: int = Form(...)):
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, status FROM member_forms WHERE id=$1", id
        )
    if not row or row["status"] != "pending":
        raise HTTPException(400, "Form not found or already handled")

    uid: int = row["user_id"]
    guild    = botmod.bot.get_guild(GUILD_ID)
    if not guild:
        raise HTTPException(503, "Discord bot not ready")

    await guild.ban(
        discord.Object(id=uid),
        reason=f"Application denied via web panel by {user} (temp-ban)",
        delete_message_seconds=0
    )

    async def unban_later():
        await asyncio.sleep(botmod.TEMP_BAN_SECONDS)
        try:
            await guild.unban(discord.Object(id=uid), reason="Temp ban expired")
        except discord.HTTPException:
            pass
    asyncio.create_task(unban_later())

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE member_forms SET status='denied' WHERE id=$1", id
        )
    return JSONResponse({"status": "denied"})


@app.post("/forms/delete")
@login_required
async def delete_form(request: Request, user: str, id: int = Form(...)):
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM member_forms WHERE id=$1", id)
    return JSONResponse({"status": "deleted"})

# ═════════════════════════════  GIVEAWAYS  ════════════════════════════
@app.post("/giveaways/update")
@login_required
async def update_giveaway(
    request: Request,
    user: str,
    id: int = Form(...),
    prize: str = Form(...),
    end_ts: int = Form(...),
    note: str = Form("")
):
    async with db.acquire() as conn:
        await conn.execute("""
            UPDATE giveaways
               SET prize=$2, end_ts=$3, note=$4
             WHERE id=$1
        """, id, prize, end_ts, note)
    return RedirectResponse("/admin#giveaways", status_code=303)


@app.post("/giveaways/end")
@login_required
async def end_giveaway(request: Request, user: str, id: int = Form(...)):
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE giveaways SET active=FALSE WHERE id=$1", id
        )
    return RedirectResponse("/admin#giveaways", status_code=303)