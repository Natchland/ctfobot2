# cogs/recruit_reminder.py
"""
Recruitment-reminder system
──────────────────────────
• One message in the recruitment channel asks for staff.
• “Accept” button assigns a 6-hour shift, displays live countdown
  “for the next X h Y m” (edited every 15 s, rate-limit friendly).
• Auto-reset after 6 h or via the /recruitreset admin command.
• State stored in Postgres → survives restarts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ─────────────────────────────────────────────────────────────
#                       CONFIG
# ─────────────────────────────────────────────────────────────
RECRUIT_CHANNEL_ID   = 1413188006499586158       # channel for the reminder
RECRUITMENT_ROLE_ID  = 1410659214959054988       # staff role that can accept

SHIFT_SECONDS        = 6 * 60 * 60               # 6-hour shift
UPDATE_INTERVAL      = 15                        # edit frequency (s)

# ─────────────────────────────────────────────────────────────
#                       SQL
# ─────────────────────────────────────────────────────────────
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS recruit_reminder (
    id          BOOLEAN PRIMARY KEY DEFAULT TRUE,
    message_id  BIGINT,
    claimed_by  BIGINT,
    end_ts      BIGINT
);
INSERT INTO recruit_reminder (id)
VALUES (TRUE)
ON CONFLICT (id) DO NOTHING;
"""

GET_SQL = "SELECT message_id, claimed_by, end_ts FROM recruit_reminder LIMIT 1"

SET_SQL = """
UPDATE recruit_reminder
SET message_id = $1,
    claimed_by = $2,
    end_ts     = $3
WHERE id = TRUE;
"""


# ═════════════════════════════════════════════════════════════
class RecruitReminder(commands.Cog):
    """Recruitment-shift workflow."""

    # ---------------------------------------------------------
    #                Cog initialisation
    # ---------------------------------------------------------
    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._table_ready = asyncio.Event()          # set once helper table exists
        self._loop_started = False                   # guard so the updater starts once

    # ---------------------------------------------------------
    #             Admin command  /recruitreset
    # ---------------------------------------------------------
    @app_commands.command(
        name="recruitreset",
        description="Force-reset the recruitment reminder (admin only)"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def recruit_reset(self, inter: discord.Interaction):
        await self._set_state(message_id=None, claimed_by=None, end_ts=None)
        await inter.response.send_message(
            "Recruitment reminder will refresh within 15 seconds.",
            ephemeral=True
        )

    # ---------------------------------------------------------
    #                Discord “Accept” button
    # ---------------------------------------------------------
    class AcceptView(discord.ui.View):
        def __init__(self, outer: "RecruitReminder"):
            super().__init__(timeout=None)
            self.outer = outer

        @discord.ui.button(
            label="Accept",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id="recruit_accept"               # required for persistence
        )
        async def accept(self, inter: discord.Interaction, _):
            guild = inter.guild
            role  = guild.get_role(RECRUITMENT_ROLE_ID)

            if not (
                inter.user.guild_permissions.administrator
                or (role and role in inter.user.roles)
            ):
                return await inter.response.send_message(
                    "You’re not recruitment staff.", ephemeral=True
                )

            end_ts = int(datetime.now(timezone.utc).timestamp()) + SHIFT_SECONDS

            await self.outer._set_state(
                message_id=inter.message.id,
                claimed_by=inter.user.id,
                end_ts=end_ts
            )

            # disable button
            for child in self.children:
                child.disabled = True

            await inter.message.edit(
                content=(
                    f"✅ {inter.user.mention} is handling recruitment for the "
                    f"next **6 hours**."
                ),
                view=self
            )
            await inter.response.send_message("Thanks for taking the shift!", ephemeral=True)

    # ---------------------------------------------------------
    #          Background updater (starts in on_ready)
    # ---------------------------------------------------------
    @tasks.loop(seconds=UPDATE_INTERVAL)
    async def update_message(self):
        await self._table_ready.wait()

        state   = await self._get_state()
        channel = self.bot.get_channel(RECRUIT_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        now_ts = int(datetime.now(timezone.utc).timestamp())

        # 1️⃣  No message yet → create prompt
        if state["message_id"] is None:
            msg = await channel.send(
                f"<@&{RECRUITMENT_ROLE_ID}> Any staff available to recruit?\n"
                "Click **Accept** below to claim the next six-hour shift.",
                view=self.AcceptView(self)
            )
            await self._set_state(message_id=msg.id, claimed_by=None, end_ts=None)
            return

        # 2️⃣  Fetch stored message; recreate if deleted
        try:
            msg = await channel.fetch_message(state["message_id"])
        except discord.NotFound:
            await self._set_state(message_id=None, claimed_by=None, end_ts=None)
            return

        # 3️⃣  Active shift?
        if state["end_ts"]:
            if now_ts >= state["end_ts"]:          # shift expired → reset
                await msg.edit(
                    content=(
                        f"<@&{RECRUITMENT_ROLE_ID}> Any staff available to recruit?\n"
                        "Click **Accept** below to claim the next six-hour shift."
                    ),
                    view=self.AcceptView(self)
                )
                await self._set_state(message_id=msg.id, claimed_by=None, end_ts=None)
            else:
                # refresh remaining time without “in”
                remaining = state["end_ts"] - now_ts
                hrs, rem  = divmod(remaining, 3600)
                mins      = rem // 60
                await msg.edit(
                    content=(
                        f"✅ <@{state['claimed_by']}> is handling recruitment "
                        f"for the next **{hrs} h {mins:02} m**."
                    )
                )
        else:
            # ensure button visible after restart
            if not msg.components:
                await msg.edit(view=self.AcceptView(self))

    # ---------------------------------------------------------
    #      Helper: create table (once) & start background loop
    # ---------------------------------------------------------
    async def _ensure_table_and_loop(self):
        """Called from on_ready until table exists and loop is started."""
        if self._table_ready.is_set():
            return

        # wait until db.connect() created the pool
        if self.db.pool is None:
            return

        async with self.db.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)

        self._table_ready.set()

        if not self._loop_started:
            self.update_message.start()
            self._loop_started = True

    # ---------------------------------------------------------
    #      Light DB wrappers with auto-table creation safety
    # ---------------------------------------------------------
    async def _get_state(self) -> dict[str, Optional[int]]:
        async with self.db.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(GET_SQL)
            except asyncpg.UndefinedTableError:
                await conn.execute(CREATE_SQL)
                row = None
        return dict(row) if row else {"message_id": None,
                                      "claimed_by": None,
                                      "end_ts": None}

    async def _set_state(self, *, message_id, claimed_by, end_ts):
        async with self.db.pool.acquire() as conn:
            try:
                await conn.execute(SET_SQL, message_id, claimed_by, end_ts)
            except asyncpg.UndefinedTableError:
                await conn.execute(CREATE_SQL)
                await conn.execute(SET_SQL, message_id, claimed_by, end_ts)

    # ---------------------------------------------------------
    #   on_ready – ensure table exists, start loop, re-attach view
    # ---------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self):
        await self._ensure_table_and_loop()        # may start the loop

        state = await self._get_state()
        if state["message_id"]:
            self.bot.add_view(self.AcceptView(self), message_id=state["message_id"])


# ─────────────────────────────────────────────────────────────
#                   setup()  entry-point
# ─────────────────────────────────────────────────────────────
async def setup(bot, db):
    await bot.add_cog(RecruitReminder(bot, db))