# cogs/codes.py
# ────────────────────────────────────────────────────────────────────
#   Access-Code system  –  /codes slash-commands + single embed
# ────────────────────────────────────────────────────────────────────
from __future__ import annotations

import os
import asyncio
import contextlib
from datetime import datetime, timezone
from typing import Dict, Optional

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

# ═════════════════════ CONFIG ═════════════════════
CODES_CH_ID  = 1398667158237483138                 # channel that holds the embed
STORE_PATH   = "/data/codes_msg_id.txt"            # remembers embed message-id
DATABASE_URL = os.getenv("DATABASE_URL")           # for LISTEN codes_changed
# ══════════════════════════════════════════════════


# ───────────────────────── Embed builder ─────────────────────────
def _build_embed(codes: Dict[str, tuple[str, bool]]) -> discord.Embed:
    e = discord.Embed(
        title="🔑 Access Codes",
        description="Codes with 🔒 are **private** (hidden from `/codes list`).",
        colour=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    if not codes:
        e.description += "\n\n*No codes configured yet.*"
    else:
        for name, (pin, public) in codes.items():
            lock = "" if public else " 🔒"
            e.add_field(name=f"{name}{lock}", value=f"`{pin}`", inline=False)
    return e


# ═════════════════════ COG ═══════════════════════
class CodesCog(commands.Cog):
    """/codes command group, single embed upkeep, Postgres listener."""

    # group is auto-registered when cog is injected
    codes_group = app_commands.Group(name="codes", description="Manage / view access codes")

    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._lock = asyncio.Lock()
        self._listener_task: Optional[asyncio.Task] = None
        self._ready = False                        # run on_ready once

    # ─────────────── CLEAN-UP ───────────────
    async def cog_unload(self):
        if self._listener_task:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task

    # ─────────────── AFTER LOGIN ───────────────
    @commands.Cog.listener()
    async def on_ready(self):
        if self._ready:
            return
        self._ready = True

        # wait until the core bot has created the asyncpg pool
        while self.db.pool is None:
            await asyncio.sleep(0.5)

        await self._refresh_embed()
        self._listener_task = asyncio.create_task(self._listen_pg())

    # ═════════════════ UTILITIES ════════════════
    async def _channel(self) -> Optional[discord.TextChannel]:
        ch = self.bot.get_channel(CODES_CH_ID)
        return ch if isinstance(ch, discord.TextChannel) else None

    @staticmethod
    def _valid_pin(pin: str) -> bool:
        return pin.isdigit() and len(pin) == 4

    async def _is_staff(self, i: discord.Interaction) -> bool:
        reviewers = await self.db.get_reviewers()
        return i.user.guild_permissions.administrator or i.user.id in reviewers

    # ═════════════ EMBED REFRESH ═══════════════
    async def _refresh_embed(self):
        async with self._lock:                     # debounce
            try:
                ch = await self._channel()
                if ch is None:
                    print("[codes] Codes channel not found!")
                    return

                # ----- find existing embed -----
                msg: Optional[discord.Message] = None
                if os.path.exists(STORE_PATH):
                    try:
                        mid = int(open(STORE_PATH).read())
                        msg = await ch.fetch_message(mid)
                    except (ValueError, discord.NotFound, discord.HTTPException):
                        msg = None
                if msg is None:
                    async for m in ch.history(limit=50):
                        if (m.author == self.bot.user
                                and m.embeds
                                and m.embeds[0].title.startswith("🔑 Access Codes")):
                            msg = m
                            break

                embed = _build_embed(await self.db.get_codes())

                if msg:
                    await msg.edit(embed=embed)
                    mid = msg.id
                else:
                    msg = await ch.send(embed=embed)
                    mid = msg.id

                os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
                with open(STORE_PATH, "w") as f:
                    f.write(str(mid))

                print(f"[codes] Embed refreshed (message {mid})")
            except Exception as exc:
                print(f"[codes] refresh error: {type(exc).__name__}: {exc}")

    # ═════════════ SLASH COMMANDS ══════════════
    @codes_group.command(name="add", description="Add a new access code")
    @app_commands.describe(name="Code name", pin="4-digit number", public="Visible in /codes list?")
    async def add_code(self, i: discord.Interaction, name: str, pin: str, public: bool = False):
        if not await self._is_staff(i):
            return await i.response.send_message("Permission denied.", ephemeral=True)
        if not self._valid_pin(pin):
            return await i.response.send_message("PIN must be 4 digits.", ephemeral=True)
        if name in await self.db.get_codes():
            return await i.response.send_message("Name already exists.", ephemeral=True)

        await self.db.add_code(name, pin, public)
        await self._refresh_embed()
        await i.response.send_message("Code added.", ephemeral=True)

    # -------------------------------------------------------
    @codes_group.command(name="edit", description="Edit an existing code")
    @app_commands.describe(name="Existing name", pin="New 4-digit pin", public="Leave blank to keep current visibility")
    async def edit_code(self, i: discord.Interaction, name: str, pin: str, public: Optional[bool] = None):
        if not await self._is_staff(i):
            return await i.response.send_message("Permission denied.", ephemeral=True)
        if not self._valid_pin(pin):
            return await i.response.send_message("PIN must be 4 digits.", ephemeral=True)
        if name not in await self.db.get_codes():
            return await i.response.send_message("No such code.", ephemeral=True)

        await self.db.edit_code(name, pin, public)
        await self._refresh_embed()
        await i.response.send_message("Code updated.", ephemeral=True)

    # -------------------------------------------------------
    @codes_group.command(name="remove", description="Delete a code")
    async def remove_code(self, i: discord.Interaction, name: str):
        if not await self._is_staff(i):
            return await i.response.send_message("Permission denied.", ephemeral=True)
        if name not in await self.db.get_codes():
            return await i.response.send_message("No such code.", ephemeral=True)

        await self.db.remove_code(name)
        await self._refresh_embed()
        await i.response.send_message("Code removed.", ephemeral=True)

    # -------------------------------------------------------
    @codes_group.command(name="list", description="Show public codes")
    async def list_codes(self, i: discord.Interaction):
        pub = await self.db.get_codes(only_public=True)
        if not pub:
            return await i.response.send_message("No public codes.", ephemeral=True)
        await i.response.send_message(
            "\n".join(f"• **{n}**: `{pin}`" for n, (pin, _) in pub.items()),
            ephemeral=True
        )

    # ═════════════ Postgres LISTEN ═════════════
    async def _listen_pg(self):
        if not DATABASE_URL:
            print("[codes] DATABASE_URL not set – listener disabled")
            return
        try:
            conn: asyncpg.Connection = await asyncpg.connect(DATABASE_URL)
            await conn.add_listener(
                "codes_changed",
                lambda *_: asyncio.create_task(self._refresh_embed())
            )
            print("[codes] LISTEN codes_changed attached")

            while True:
                await asyncio.sleep(3600)          # keep task alive
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"[codes] listener error: {type(exc).__name__}: {exc}")
        finally:
            with contextlib.suppress(Exception):
                await conn.close()


# ═════════════ setup entry-point ═════════════
async def setup(bot: commands.Bot, db):
    await bot.add_cog(CodesCog(bot, db))