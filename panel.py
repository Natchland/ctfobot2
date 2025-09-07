app = FastAPI(debug=True)
from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
import asyncpg
import os
from datetime import datetime


DATABASE_URL = os.getenv("DATABASE_URL")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

db_pool = None

@app.on_event("startup")
async def startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)

@app.get("/")
async def index(request: Request):
    async with db_pool.acquire() as conn:
        codes = await conn.fetch("SELECT name, pin FROM codes ORDER BY name")
    # Pass 'year' to the template!
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "codes": codes,
            "year": datetime.now().year,
        },
    )

@app.post("/codes/add")
async def add_code(name: str = Form(...), pin: str = Form(...)):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO codes (name, pin) VALUES ($1, $2) ON CONFLICT (name) DO UPDATE SET pin = $2",
            name,
            pin,
        )
    return RedirectResponse("/", status_code=303)

@app.post("/codes/remove")
async def remove_code(name: str = Form(...)):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM codes WHERE name=$1", name)
    return RedirectResponse("/", status_code=303)