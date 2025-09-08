import os, datetime, asyncpg, httpx, inspect
from pathlib import Path
from itsdangerous import URLSafeSerializer, BadSignature
from passlib.context import CryptContext
from fastapi import FastAPI, Request, Form, Response, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ─────────────────────── Config ───────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")
WEB_SECRET   = os.getenv("WEB_SECRET", "CHANGE_ME")
OWNER_KEY    = os.getenv("OWNER_KEY",  "OWNER_ONLY")
COOKIE_NAME  = "ctfo_admin"

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
signer  = URLSafeSerializer(WEB_SECRET)

# ────────────────────── FastAPI ───────────────────────
app       = FastAPI(debug=False)
templates = Jinja2Templates(directory="templates")

static_path = Path("static")
if static_path.exists():
    app.mount("/static", StaticFiles(directory=static_path), name="static")

db: asyncpg.Pool | None = None

# ────────────────── DB startup / migration ─────────────
@app.on_event("startup")
async def startup():
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
            pin TEXT NOT NULL,
            public BOOLEAN NOT NULL DEFAULT FALSE
        );""")
        # NOTE: member_forms & giveaways tables already created by the bot

# ────────────────── Helper to load dashboard data ──────
async def all_admin_data():
    async with db.acquire() as conn:
        codes = await conn.fetch("SELECT * FROM codes ORDER BY name")
        forms = await conn.fetch("SELECT * FROM member_forms ORDER BY created_at DESC")
        gws   = await conn.fetch("SELECT * FROM giveaways ORDER BY end_ts DESC")
    return codes, forms, gws

# ────────────────── Auth helpers (unchanged) ───────────
#       current_user(), login_required()  …  etc.
#       Signup / login / logout endpoints remain as you had them
# --------------------------------------------------------------------

# ────────────────── Admin dashboard ─────────────────────
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

# ──────────────── Code management (unchanged) ───────────
#     /codes/add  /codes/remove  …  (keep them)

# ═══════════════ MEMBER-FORMS  (NEW) ════════════════════
@app.post("/forms/update")
@login_required
async def update_form(request: Request, user: str,
                      id: int = Form(...), json: str = Form(...)):
    import json as _j
    try: _j.loads(json)
    except Exception: raise HTTPException(400, "Not valid JSON.")
    async with db.acquire() as conn:
        await conn.execute("UPDATE member_forms SET data=$2 WHERE id=$1", id, json)
    return RedirectResponse("/admin#forms", status_code=303)

@app.post("/forms/delete")
@login_required
async def delete_form(request: Request, user: str, id: int = Form(...)):
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM member_forms WHERE id=$1", id)
    return RedirectResponse("/admin#forms", status_code=303)

# ═══════════════ GIVEAWAYS   (NEW) ══════════════════════
@app.post("/giveaways/update")
@login_required
async def update_giveaway(request: Request, user: str,
                          id: int = Form(...),
                          prize: str = Form(...),
                          end_ts: int = Form(...),
                          note: str = Form("")):
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
        await conn.execute("UPDATE giveaways SET active=FALSE WHERE id=$1", id)
    return RedirectResponse("/admin#giveaways", status_code=303)

# ────────────────── Public page ────────────────────────
@app.get("/", response_class=HTMLResponse)
async def welcome(request: Request):
    guild_id     = os.getenv("GUILD_ID")
    member_count = "?"
    if guild_id:
        try:
            async with httpx.AsyncClient() as cli:
                r = await cli.get(
                    f"https://discord.com/api/guilds/{guild_id}/widget.json",
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

# ────────────────── Sign-up / Login / Logout ───────────
@app.get("/signup", response_class=HTMLResponse)
async def signup_get(request: Request):
    return templates.TemplateResponse("signup.html", {"request": request})

@app.post("/signup")
async def signup_post(username: str = Form(...), password: str = Form(...)):
    hash_ = pwd_ctx.hash(password)
    async with db.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO admins (username, pwd_hash, approved) VALUES ($1,$2,FALSE)",
                username, hash_
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=400, detail="Username taken.")
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
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM admins WHERE username=$1", username)
    if not row or not row["approved"] or not pwd_ctx.verify(password, row["pwd_hash"]):
        raise HTTPException(status_code=403,
                            detail="Invalid credentials or not yet approved.")

    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        COOKIE_NAME, signer.dumps(username),
        httponly=True, max_age=86400 * 7
    )
    return response

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp

# ────────────────── Owner approval endpoint ────────────
@app.post("/approve")
async def approve_user(request: Request, username: str = Form(...)):
    if request.headers.get("X-OWNER-KEY") != OWNER_KEY:
        raise HTTPException(status_code=403, detail="Bad owner key.")
    async with db.acquire() as conn:
        await conn.execute("UPDATE admins SET approved=TRUE WHERE username=$1", username)
    return "approved"

# ────────────────── Admin dashboard ─────────────────────
@app.get("/admin", response_class=HTMLResponse)
@login_required
async def admin_panel(request: Request, user: str):
    async with db.acquire() as conn:
        codes = await conn.fetch(
            "SELECT name, pin, public FROM codes ORDER BY name"
        )
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "codes": codes,
            "user": user,
            "year": datetime.datetime.now().year,
        },
    )

# ──────────────── Code management (protected) ───────────
@app.post("/codes/add")
@login_required
async def add_code(
    request: Request, user: str,
    name: str = Form(...),
    pin: str  = Form(...),
    public: str | None = Form(None)
):
    if not (pin.isdigit() and len(pin) == 4):
        raise HTTPException(status_code=400, detail="Pin must be 4 digits.")
    async with db.acquire() as conn:
        await conn.execute("""
        INSERT INTO codes (name, pin, public)
        VALUES ($1,$2,$3)
        ON CONFLICT(name) DO UPDATE SET pin=$2, public=$3
        """, name, pin, bool(public))
    return RedirectResponse("/admin", status_code=303)

@app.post("/codes/remove")
@login_required
async def remove_code(
    request: Request, user: str,
    name: str = Form(...)
):
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM codes WHERE name=$1", name)
    return RedirectResponse("/admin", status_code=303)