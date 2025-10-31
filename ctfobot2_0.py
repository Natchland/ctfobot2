# ctfobot2_0.py – CTFO Discord bot (core launcher)
# =================================================
from __future__ import annotations

import asyncio
import logging
import os
import sys
from importlib import import_module
from types import ModuleType
from typing import Sequence

import discord
from discord.ext import commands
from dotenv import load_dotenv

from db import Database

# ─────────────────────────── log / env ────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
    force=True,
)

BOT_TOKEN: str | None = os.getenv("BOT_TOKEN")
DATABASE_URL: str | None = os.getenv("DATABASE_URL")
if not BOT_TOKEN or not DATABASE_URL:
    raise RuntimeError("Set BOT_TOKEN and DATABASE_URL in .env!")

# ─────────────────────────── database ─────────────────────────────
db = Database(DATABASE_URL)

# ─────────────────────────── shared constants ─────────────────────
GUILD_ID = int(os.getenv("GUILD_ID", "1377035207777194005"))

WELCOME_CHANNEL_ID      = 1398659438960971876
APPLICATION_CH_ID       = 1378081331686412468
UNCOMPLETED_APP_ROLE_ID = 1390143545066917931
COMPLETED_APP_ROLE_ID   = 1398708167525011568
ACCEPT_ROLE_ID          = 1377075930144571452

REGION_ROLE_IDS = {
    "North America": 1411364406096433212,
    "Europe":        1411364744484491287,
    "Asia":          1411364982117105684,
    "Other":         1411365034440921260,
}
FOCUS_ROLE_IDS = {
    "Farming":      1379918816871448686,
    "Base Sorting": 1400849292524130405,
    "Building":     1380233086544908428,
    "Electricity":  1380233234675400875,
    "PvP":          1408687710159245362,
}

TEMP_BAN_SECONDS  = 7 * 24 * 60 * 60
GIVEAWAY_ROLE_ID  = 1403337937722019931
GIVEAWAY_CH_ID    = 1413929735658016899
EMBED_TITLE       = "🎉 GIVEAWAY 🎉"

ADMIN_ID        = 1377103244089622719
ELECTRICIAN_ID  = 1380233234675400875
GROUP_LEADER_ID = 1377077466513932338
PLAYER_MGMT_ID  = 1377084533706588201
TRUSTED_ID      = 1400584430900219935

CODE_NAMES = ["Master", "Guest", "Electrician", "Other"]

STAFF_BONUS_ROLE_IDS = {
    ADMIN_ID,
    GROUP_LEADER_ID,
    PLAYER_MGMT_ID,
    1410659214959054988,  # recruitment
}

BOOST_BONUS_PER_WEEK = 3
STAFF_BONUS_PER_WEEK = 3
STREAK_BONUS_PER_SET = 3

# ─────────────────────────── bot instance ─────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.messages = True
intents.message_content = True  # needed for XP + moderation

bot = commands.Bot(command_prefix="!", intents=intents)

# ─────────────────────────── slash-cmd error ──────────────────────
@bot.tree.error
async def app_command_error(inter: discord.Interaction, err: Exception):
    logging.error("Slash-cmd error: %s – %s", type(err).__name__, err)

# ─────────────────────────── on_ready (sync) ──────────────────────
@bot.event
async def on_ready() -> None:
    logging.info("Logged in as %s (%s)", bot.user, bot.user.id)

    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    logging.info("Slash-commands synced for guild %s", GUILD_ID)

# ─────────────────────────── helper: cog loader ───────────────────
async def load_cogs(bot_: commands.Bot, db_: Database, paths: Sequence[str]) -> None:
    for dotted in paths:
        try:
            module: ModuleType = import_module(dotted)
            if not hasattr(module, "setup"):
                logging.warning("Module %s has no setup() – skipped", dotted)
                continue
            await module.setup(bot_, db_)
            logging.info("Loaded cog %s", dotted)
        except Exception:
            logging.exception("Failed to load cog %s", dotted)

# ─────────────────────────── main runner ──────────────────────────
async def _run_bot() -> None:
    await db.connect()                      # 1) DB
    await load_cogs(                        # 2) Cogs
        bot,
        db,
        (
            "cogs.codes",
            "cogs.feedback",
            "cogs.member_forms",
            "cogs.recruit_reminder",
            "cogs.staff_applications",
            "cogs.stats",
            "cogs.steam_sync",
            "cogs.welcome_general",
            "cogs.welcome_member",
            "cogs.xp",
        ),
    )
    await bot.start(BOT_TOKEN)              # 3) live

# ─────────────────────────── entry-point ──────────────────────────
def main() -> None:
    try:
        asyncio.run(_run_bot())
    except KeyboardInterrupt:
        logging.info("Bot stopped by user")
    finally:
        # Close the DB pool cleanly
        try:
            asyncio.run(db.close())  # type: ignore[arg-type]
        except RuntimeError:
            # event-loop already closed (windows quirk)
            pass


if __name__ == "__main__":
    main()