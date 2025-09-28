# cogs/recruit_reminder.py
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ───────────────────────────── CONFIG ──────────────────────────────
RECRUIT_CHANNEL_ID   = 1421856820460388383      # channel that shows the prompt
RECRUITMENT_ROLE_ID  = 1410659214959054988      # role allowed to click “Accept”

SHIFT_SECONDS        = 6 * 60 * 60              # 6-hour recruitment shift
UPDATE_INTERVAL      = 15                       # edit frequency (seconds)

# ───────────────────────────── SQL ─────────────────────────────────
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS recruit_reminder (
    id          BOOLEAN PRIMARY KEY DEFAULT TRUE,
    message_id  BIGINT,
    claimed_by  BIGINT,
    end_ts      BIGINT
);
INSERT INTO recruit_reminder (id) VALUES (TRUE)
ON CONFLICT (id) DO NOTHING;
"""
GET_SQL = "SELECT message_id, claimed_by, end_ts FROM recruit_reminder LIMIT 1"
SET_SQL = """
UPDATE recruit_reminder
SET message_id = $1, claimed_by = $2, end_ts = $3
WHERE id = TRUE;
"""


# ═════════════════════════════ COG ════════════════════════════════
class RecruitReminder(commands.Cog):
    """Recruit-shift workflow with 6-hour windows and auto-reset."""

    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._table_ready = asyncio.Event()

        # updater starts immediately but blocks on _table_ready until DB ready
        self.update_message.start()

    # ─────────────────────────── DB helpers ────────────────────────────
    async def _get_state(self) -> dict[str, Optional[int]]:
        async with self.db.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(GET_SQL)
            except asyncpg.UndefinedTableError:
                await conn.execute(CREATE_SQL)
                row = None
        return dict(row) if row else {
            "message_id": None,
            "claimed_by": None,
            "end_ts":     None,
        }

    async def _set_state(self, *, message_id, claimed_by, end_ts):
        async with self.db.pool.acquire() as conn:
            try:
                await conn.execute(SET_SQL, message_id, claimed_by, end_ts)
            except asyncpg.UndefinedTableError:
                await conn.execute(CREATE_SQL)
                await conn.execute(SET_SQL, message_id, claimed_by, end_ts)

    # ────────────────────── Accept-button view ─────────────────────────
    class AcceptView(discord.ui.View):
        def __init__(self, outer: "RecruitReminder"):
            super().__init__(timeout=None)
            self.outer = outer

        @discord.ui.button(
            label="Accept",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id="recruit_accept"
        )
        async def accept(self, inter: discord.Interaction, _):
            guild = inter.guild
            staff_role = guild.get_role(RECRUITMENT_ROLE_ID)

            if not (
                inter.user.guild_permissions.administrator
                or (staff_role and staff_role in inter.user.roles)
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
            await inter.response.send_message("Shift accepted – thank you!", ephemeral=True)

    # ───────────────────── 15-second updater loop ──────────────────────
    @tasks.loop(seconds=UPDATE_INTERVAL)
    async def update_message(self):
        await self._table_ready.wait()                       # wait until table exists

        state   = await self._get_state()
        channel = self.bot.get_channel(RECRUIT_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        now_ts = int(datetime.now(timezone.utc).timestamp())

        # 1️⃣  no message stored → create prompt
        if state["message_id"] is None:
            msg = await channel.send(
                f"<@&{RECRUITMENT_ROLE_ID}> Any staff available to recruit?\n"
                "Click **Accept** below to claim the next six-hour shift.",
                view=self.AcceptView(self)
            )
            await self._set_state(message_id=msg.id, claimed_by=None, end_ts=None)
            return

        # 2️⃣  fetch message; recreate if deleted
        try:
            msg = await channel.fetch_message(state["message_id"])
        except discord.NotFound:
            await self._set_state(message_id=None, claimed_by=None, end_ts=None)
            return

        # 3️⃣  active shift?
        if state["end_ts"]:
            if now_ts >= state["end_ts"]:                  # expired → reset
                await msg.edit(
                    content=(
                        f"<@&{RECRUITMENT_ROLE_ID}> Any staff available to recruit?\n"
                        "Click **Accept** below to claim the next six-hour shift."
                    ),
                    view=self.AcceptView(self)
                )
                await self._set_state(message_id=msg.id, claimed_by=None, end_ts=None)
            else:
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

    # ───────────────────  create table once (on_ready) ─────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        # first on_ready call after login → create helper table
        if not self._table_ready.is_set():
            if self.db.pool:                             # db.connect() already ran
                async with self.db.pool.acquire() as conn:
                    await conn.execute(CREATE_SQL)
                self._table_ready.set()

        # re-attach persistent view so buttons work after restart
        state = await self._get_state()
        if state["message_id"]:
            self.bot.add_view(self.AcceptView(self), message_id=state["message_id"])

    # ───────────────────── /recruitreset command ───────────────────────
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

# ─────────────────────────── setup hook ─────────────────────────────
async def setup(bot, db):
    await bot.add_cog(RecruitReminder(bot, db))